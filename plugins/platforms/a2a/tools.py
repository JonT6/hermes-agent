"""
A2A client tools — let the Hermes agent talk to *other* agents as a peer.

Tools (registered in the ``a2a`` toolset):
  - a2a_discover(url)         -> fetch + summarize a peer's Agent Card
  - a2a_call(agent, message)  -> send a task to a peer, return its reply
  - a2a_list()                -> list configured peers + persisted conversations
  - a2a_history(context_id)   -> recall a persisted A2A conversation
  - a2a_orchestrate(...)      -> fan-out task to multiple peers by capability

Peers are resolved from config.yaml under ``a2a_agents``::

    a2a_agents:
      researcher:
        url: "http://localhost:9999"
        auth: { type: bearer, token: "sk-..." }
        timeout: 120
        capabilities: [web_search, research]

Transport is stdlib urllib (no a2a-sdk dependency). The wire format is the A2A
v1.0 JSON-RPC ``message/send`` method; replies from v0.3 peers still parse.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, TypedDict

from . import protocol, security

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120

# How long we wait for a peer to FINISH, as opposed to how long a single HTTP
# request may take (`timeout`). Conflating those two is what made this tool fire
# and forget: a peer that correctly returns immediately in `working` — leo-a2a
# does, deliberately, because a triage takes minutes — was treated as having
# answered, and its actual result was never collected by anyone.
#
# 300s is a compromise, not a law. leo-a2a's own wall clock is 900s, so a long
# triage outlives this and takes the loud "still running" path with a task id to
# collect later. Waiting the full 900 would pin an operator's Telegram turn for
# fifteen minutes; waiting 120 (the HTTP timeout) would abandon most real work.
# Raise it per peer with `poll_timeout` where nobody is waiting, e.g. cron.
_DEFAULT_POLL_TIMEOUT_S = 300
_POLL_INTERVAL_S = 3

# States where the peer has stopped talking *for now*. `input-required` and
# `auth-required` are terminal FOR THE CALLER even though the task is unfinished:
# polling them would wait forever on input only we can supply.
_TERMINAL_STATES = frozenset({
    protocol.STATE_COMPLETED,
    protocol.STATE_FAILED,
    protocol.STATE_CANCELED,
    protocol.STATE_REJECTED,
    protocol.STATE_INPUT_REQUIRED,
    protocol.STATE_AUTH_REQUIRED,
})
_ORCHESTRATE_MAX_WORKERS = 6  # max parallel peers for fan-out


# --------------------------------------------------------------------------
# Peer resolution
# --------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_peer(agent: str) -> Optional[dict]:
    """Resolve a peer name to {url, auth, timeout, capabilities}, or treat ``agent`` as a URL."""
    if agent.startswith("http://") or agent.startswith("https://"):
        return {"url": agent, "auth": {}, "timeout": _DEFAULT_TIMEOUT,
                "poll_timeout": _DEFAULT_POLL_TIMEOUT_S, "capabilities": []}
    cfg = _load_config()
    peers = cfg.get("a2a_agents") or {}
    entry = peers.get(agent)
    if not entry:
        return None
    return {
        "url": entry.get("url", ""),
        "auth": entry.get("auth", {}) or {},
        "timeout": int(entry.get("timeout", _DEFAULT_TIMEOUT)),
        "poll_timeout": float(entry.get("poll_timeout", _DEFAULT_POLL_TIMEOUT_S)),
        "capabilities": entry.get("capabilities", []) or [],
        "tenant": entry.get("tenant", ""),
    }


def _auth_header(auth: dict) -> dict:
    if auth and auth.get("type") == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    return {}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _http_get_json(url: str, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured peers)
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "A2A-Version": protocol.PROTOCOL_VERSION, **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured peers)
        return json.loads(resp.read().decode("utf-8"))


def _card_url(base_url: str) -> str:
    # A2A v1.0 canonical discovery path. v0.2 used agent.json; servers may
    # still serve that as a legacy alias, but clients should prefer this.
    return base_url.rstrip("/") + "/.well-known/agent-card.json"


def _legacy_card_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/.well-known/agent.json"


def _fetch_card(base_url: str, headers: dict, timeout: int) -> dict:
    try:
        return _http_get_json(_card_url(base_url), headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return _http_get_json(_legacy_card_url(base_url), headers, timeout)


def _select_jsonrpc_interface(card: Optional[dict]) -> Optional[dict]:
    if isinstance(card, dict):
        for iface in card.get("supportedInterfaces", []) or []:
            if isinstance(iface, dict) and iface.get("protocolBinding") == "JSONRPC" and iface.get("url"):
                return iface
    return None


def _rpc_url(base_url: str, card: Optional[dict]) -> str:
    """Prefer the card's JSONRPC interface (v1.0 supportedInterfaces), then the
    card's legacy top-level url, then the configured base."""
    iface = _select_jsonrpc_interface(card)
    if iface:
        return str(iface["url"])
    if isinstance(card, dict) and isinstance(card.get("url"), str) and card["url"]:
        return card["url"]
    return base_url.rstrip("/")


def _interface_tenant(card: Optional[dict], peer: dict) -> str:
    iface = _select_jsonrpc_interface(card)
    if iface and iface.get("tenant"):
        return str(iface["tenant"])
    return str(peer.get("tenant") or "")


# --------------------------------------------------------------------------
# Shared send path (used by a2a_call and a2a_orchestrate)
# --------------------------------------------------------------------------

def _short_state(state: str) -> str:
    """TASK_STATE_COMPLETED -> completed (also passes through v0.3 states)."""
    return state.replace("TASK_STATE_", "").replace("_", "-").lower() if state else ""


def _poll_until_terminal(rpc_url: str, headers: dict, task_id: str,
                         timeout: int, budget: float) -> tuple[Optional[dict], str]:
    """Poll ``GetTask`` until the task stops moving, or the budget runs out.

    Returns ``(payload, state)``; a payload of ``None`` means the budget expired
    with the task still in flight. That is NOT a failure and the caller must not
    render it as an empty reply — the peer is still working and the result is
    still collectable by task id.

    A transport blip mid-poll is skipped rather than treated as a failed task:
    the work is running on the peer regardless of whether one status request
    made it there.
    """
    deadline = time.monotonic() + budget
    state = protocol.STATE_WORKING
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        body = {"jsonrpc": "2.0", "id": protocol.new_task_id(),
                "method": "GetTask", "params": {"id": task_id}}
        try:
            resp = _http_post_json(rpc_url, body, headers, timeout)
        except Exception:
            continue
        if "error" in resp:
            continue
        payload = protocol.unwrap_send_message_response(resp.get("result", {}))
        if isinstance(payload, dict):
            state = (payload.get("status") or {}).get("state", state)
            if state in _TERMINAL_STATES:
                return payload, state
    return None, state


def _send_task(agent_label: str, peer: dict, message: str, context_id: str) -> tuple[str, str, str]:
    """Send one message/send to a peer. Returns (reply_text, context_id, state).

    Raises urllib errors / ValueError for the caller to format. Handles
    outbound redaction, audit, persistence, and metrics.
    """
    base_url = peer.get("url", "")
    headers = _auth_header(peer.get("auth", {}) or {})
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))

    # Best-effort card fetch (to learn the rpc URL); non-fatal on failure.
    card = None
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30))
    except Exception:
        pass

    ctx = context_id or protocol.new_context_id()
    safe_message = security.redact_outbound(message)
    # v1.0: contextId lives inside the Message, not at the params top level.
    rpc_body = {
        "jsonrpc": "2.0",
        "id": protocol.new_task_id(),
        "method": "SendMessage",
        "params": {
            "message": protocol.text_message(protocol.ROLE_USER, safe_message, context_id=ctx),
        },
    }

    tenant = _interface_tenant(card, peer)
    if tenant:
        rpc_body["params"]["tenant"] = tenant

    # AIA-19 / D2: NOTHING is recorded as sent until the wire says so. This block
    # used to run BEFORE the POST below, so a message that never left — a wrong
    # bearer, a peer that was down — still wrote an "outbound" row, while the audit
    # log is supposed to be the source of truth for "did this get out".
    #
    # The reorder alone is NOT enough, and that is the part the spec understated.
    # A successful POST means the peer's SERVER accepted it; it does not mean the
    # peer DID it. The 2026-08-13 incident is the proof: HTTP 200, a real task id,
    # and the task failed seven seconds later on the peer's own auth. Auditing
    # "sent" at 200 would have swapped a lie about transmission for a quieter lie
    # about delivery. So the row written here says `relay.queued`, and only the
    # terminal-state check below may upgrade it to `relay.delivered`.
    rpc_url = _rpc_url(base_url, card)
    try:
        resp = _http_post_json(rpc_url, rpc_body, headers, timeout)
    except Exception as exc:
        # It never left. Record that as its own outcome rather than staying silent:
        # a missing row is indistinguishable from "nobody looked".
        code = getattr(exc, "code", None)
        security.audit("outbound", agent_label, rpc_body["id"],
                       f"transport failed before delivery: {type(exc).__name__}"
                       + (f" HTTP {code}" if code else ""),
                       outcome="relay.failed")
        raise
    if "error" in resp:
        err = resp["error"]
        security.audit("outbound", agent_label, rpc_body["id"],
                       f"peer returned a JSON-RPC error: {err.get('message', err)}",
                       outcome="relay.failed")
        raise ValueError(f"Peer '{agent_label}' returned an error: {err.get('message', err)}")

    security.audit("outbound", agent_label, rpc_body["id"], safe_message,
                   outcome="relay.queued")
    protocol.persist_message(ctx, "user", safe_message, rpc_body["id"])
    protocol.metrics.outbound_total += 1

    result = resp.get("result", {})
    payload = protocol.unwrap_send_message_response(result)
    reply_ctx, state, task_id = ctx, "", ""
    if isinstance(payload, dict):
        reply_ctx = payload.get("contextId", ctx)
        state = (payload.get("status") or {}).get("state", "")
        task_id = payload.get("id", "")

    # Collect the ANSWER, not the acknowledgement. A peer that returns
    # immediately in `working` has told us nothing yet, and returning here is
    # what made this tool fire-and-forget (AIA-12).
    if task_id and state and state not in _TERMINAL_STATES:
        budget = float(peer.get("poll_timeout", _DEFAULT_POLL_TIMEOUT_S))
        polled, state = _poll_until_terminal(rpc_url, headers, task_id, timeout, budget)
        if polled is None:
            # Loud and actionable. The old behaviour rendered this as
            # "(no text reply)", which reads as "the peer had nothing to say"
            # rather than "the peer is still working".
            reply = (
                f"Still running after {budget:g}s — '{agent_label}' has not "
                f"finished task {task_id}. This is not a failure: the peer is "
                f"still working. Collect the result with "
                f"a2a_result(agent='{agent_label}', task_id='{task_id}')."
            )
            protocol.persist_message(reply_ctx, "agent", reply, rpc_body["id"])
            protocol.metrics.inbound_total += 1
            return reply, reply_ctx, state
        payload = polled
        if isinstance(payload, dict):
            reply_ctx = payload.get("contextId", reply_ctx)

    reply = _reply_text_from_result(payload)
    # AIA-19 / D2: the ONLY place allowed to say "delivered", because it is the only
    # place that has seen a terminal state. A task that terminated FAILED is recorded
    # as such even though the transport succeeded perfectly — that distinction is the
    # whole point, and conflating the two is what sent an operator after a phantom
    # auth problem on 2026-08-13. The `relay.queued` row written at acceptance stays;
    # this one supersedes it, so the pair reads as a history rather than a verdict.
    security.audit("outbound", agent_label, task_id or rpc_body["id"],
                   f"peer task terminal in state {_short_state(state) or 'unknown'}",
                   outcome=("relay.failed" if state == protocol.STATE_FAILED
                            else "relay.delivered"))
    protocol.persist_message(reply_ctx, "agent", reply, rpc_body["id"])
    protocol.metrics.inbound_total += 1
    return reply, reply_ctx, state


def _reply_text_from_result(result: Any) -> str:
    result = protocol.unwrap_send_message_response(result)
    if not isinstance(result, dict):
        return str(result)
    # Artifacts first (final output), then status message (interim/clarify).
    for artifact in result.get("artifacts", []) or []:
        txt = protocol.extract_text(artifact)
        if txt:
            return txt
    status = result.get("status", {}) or {}
    msg = status.get("message")
    if msg:
        return protocol.extract_text(msg)
    # Bare message result (message/send may return a Message instead of a Task)
    return protocol.extract_text(result)


# --------------------------------------------------------------------------
# Tool handlers
# --------------------------------------------------------------------------

def a2a_discover(args: dict, **_: Any) -> str:
    """Fetch and summarize the Agent Card at ``url``."""
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: 'url' is required (e.g. http://localhost:9999)."
    try:
        card = _fetch_card(url, {}, _DEFAULT_TIMEOUT)
    except urllib.error.HTTPError as e:
        return f"Error: discovery failed — HTTP {e.code} from {url}."
    except Exception as e:
        return f"Error: could not reach {url} — {e}."

    name = card.get("name", "?")
    desc = card.get("description", "")
    caps = card.get("capabilities", {}) or {}
    skills = card.get("skills", []) or []
    auth = "yes" if card.get("security") else "no"
    ifaces = card.get("supportedInterfaces", []) or []
    proto = ", ".join(
        f"{i.get('protocolBinding', '?')} v{i.get('protocolVersion', '?')}"
        for i in ifaces if isinstance(i, dict)
    ) or f"v{card.get('protocolVersion', '?')} (pre-1.0 card)"
    lines = [
        f"Agent: {name}",
        f"Description: {desc}",
        f"URL: {_rpc_url(url, card)}",
        f"Protocol: {proto}",
        f"Streaming: {bool(caps.get('streaming'))}  Push: {bool(caps.get('pushNotifications'))}  Auth required: {auth}",
        f"Skills ({len(skills)}):",
    ]
    for s in skills[:20]:
        lines.append(f"  - {s.get('name', s.get('id', '?'))}: {s.get('description', '')}")
    return "\n".join(lines)


def a2a_call(args: dict, **_: Any) -> str:
    """Send a task to a peer agent and return its reply.

    Blocks until the peer finishes, polling ``GetTask`` when it accepts the task
    asynchronously (the normal case for work that takes minutes). If it outlives
    ``poll_timeout`` the return says so explicitly and carries the task id for
    ``a2a_result`` — it is never a silent empty reply.

    ``agent`` is a configured peer name (from ``a2a_agents``) or a direct URL.
    ``context_id`` continues a prior exchange (multi-turn) when provided.
    """
    # Accept common aliases models reach for (observed live: 'agent_name').
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not agent or not message:
        return "Error: both 'agent' and 'message' are required."

    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return (
            f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in "
            f"config.yaml or pass a full http(s):// URL."
        )

    try:
        reply, reply_ctx, state = _send_task(agent, peer, message, context_id)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"Error: peer '{agent}' rejected auth (HTTP {e.code}). Check the configured token."
        if e.code == 429:
            return f"Error: peer '{agent}' rate limited us (HTTP 429). Retry later."
        return f"Error: call to '{agent}' failed — HTTP {e.code}."
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: call to '{agent}' failed — {e}."

    header = f"[{agent} · context {reply_ctx}"
    if state:
        header += f" · {_short_state(state)}"
    header += "]"
    body = reply or "(no text reply)"
    if state == protocol.STATE_INPUT_REQUIRED:
        body += (
            "\n\n(The peer needs more input — answer by calling a2a_call again "
            f"with context_id '{reply_ctx}'.)"
        )
    if state == protocol.STATE_FAILED:
        # AIA-19 / D1. Everything above this point already succeeded: the request
        # reached the peer, the bearer was accepted, a task was created. What failed
        # is the PEER'S OWN processing — and the returned artifact is the peer's error
        # text, which on 2026-08-13 read "Failed to authenticate. API Error: 401 OAuth
        # access token has been revoked."
        #
        # Rendered as bare `[leo · context … · failed] Failed to authenticate…`, that is
        # visually indistinguishable from THIS tool's own auth failure a few lines up
        # ("Error: peer '…' rejected auth (HTTP 401). Check the configured token."). An
        # operator read the peer's dead credential as ours and spent ~30 minutes
        # checking a bearer that was working perfectly the whole time.
        #
        # A state token alone does not fix that: "failed" does not say WHOSE. So say it
        # in words. The header is machine-ish and easy to skim past; this is the line
        # that stops the wrong investigation before it starts.
        body = (
            "⚠️ This is the PEER's failure, not ours.\n"
            f"Our request reached '{agent}' and was accepted — transport and auth to "
            "the peer are fine. The peer's own task then failed, and the text below is "
            "ITS error, reported verbatim. Nothing here indicates a problem with our "
            "bearer token or configuration; look at the peer's side.\n\n"
            f"{body}"
        )
    return f"{header}\n{body}"


def a2a_result(args: dict, **_: Any) -> str:
    """Collect a task started earlier by ``a2a_call``.

    The escape hatch for work that outlives the poll budget: ``a2a_call`` hands
    back a task id, and this fetches it whenever the caller comes back.
    """
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    task_id = str(args.get("task_id") or args.get("taskId") or args.get("id") or "").strip()
    if not agent or not task_id:
        return "Error: both 'agent' and 'task_id' are required."

    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return (
            f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in "
            f"config.yaml or pass a full http(s):// URL."
        )

    base_url = peer.get("url", "")
    headers = _auth_header(peer.get("auth", {}) or {})
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))
    card = None
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30))
    except Exception:
        pass

    body = {"jsonrpc": "2.0", "id": protocol.new_task_id(),
            "method": "GetTask", "params": {"id": task_id}}
    try:
        resp = _http_post_json(_rpc_url(base_url, card), body, headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"Error: peer '{agent}' rejected auth (HTTP {e.code}). Check the configured token."
        return f"Error: call to '{agent}' failed — HTTP {e.code}."
    except Exception as e:
        return f"Error: call to '{agent}' failed — {e}."

    if "error" in resp:
        err = resp["error"]
        return f"Error: peer '{agent}' returned an error: {err.get('message', err)}"

    payload = protocol.unwrap_send_message_response(resp.get("result", {}))
    state = ""
    if isinstance(payload, dict):
        state = (payload.get("status") or {}).get("state", "")
    text = _reply_text_from_result(payload) or "(no text yet — still working)"
    return f"[{agent} · task {task_id} · {_short_state(state)}]\n{text}"


def a2a_list(args: dict | None = None, **_: Any) -> str:
    """List configured A2A peers and any persisted conversations."""
    cfg = _load_config()
    peers = cfg.get("a2a_agents") or {}
    lines = []
    if peers:
        lines.append(f"Configured peers ({len(peers)}):")
        for name, entry in peers.items():
            auth = (entry.get("auth") or {}).get("type", "none")
            caps = entry.get("capabilities", [])
            cap_str = f" caps: {', '.join(caps)}" if caps else ""
            lines.append(f"  - {name}: {entry.get('url', '?')} (auth: {auth}){cap_str}")
    else:
        lines.append("No peers configured. Add them under 'a2a_agents' in config.yaml.")

    convos = protocol.list_conversations()
    if convos:
        lines.append("")
        lines.append(f"Persisted conversations ({len(convos)}) — recall with a2a_history:")
        for c in convos[:25]:
            lines.append(f"  - {c}")

    # Show metrics snapshot
    m = protocol.metrics.snapshot()
    lines.append("")
    lines.append(f"Metrics: {m['inbound_total']} in / {m['outbound_total']} out, "
                 f"{m['tasks_completed']} completed, {m['tasks_failed']} failed, "
                 f"{m['streams_started']} streams, {m['push_sent']} push sent, "
                 f"{m['anti_loop_triggers']} anti-loop, {m['rate_limit_triggers']} rate-limited, "
                 f"avg {m['avg_latency_ms']}ms")

    return "\n".join(lines)


def a2a_history(args: dict, **_: Any) -> str:
    """Recall a persisted A2A conversation by context_id.

    This is how prior A2A exchanges survive compaction/restarts: every turn is
    written to ~/.hermes/a2a_conversations/<context>.jsonl and can be reloaded
    here.
    """
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not context_id:
        return "Error: 'context_id' is required (see a2a_list for known conversations)."
    try:
        limit = max(1, min(int(args.get("limit") or 50), 200))
    except (ValueError, TypeError):
        limit = 50
    messages = protocol.load_conversation(context_id, limit=limit)
    if not messages:
        return f"No persisted conversation for context '{context_id}'."
    lines = [f"Conversation {context_id} (last {len(messages)} messages):"]
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").strip()
        if len(text) > 1000:
            text = text[:1000] + " …[truncated]"
        lines.append(f"[{role}] {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# a2a_orchestrate: capability-based routing with fan-out
# --------------------------------------------------------------------------

def _match_peers_by_capability(capability: str) -> list[tuple[str, dict]]:
    """Find configured peers that advertise the given capability."""
    cfg = _load_config()
    peers = cfg.get("a2a_agents") or {}
    matches = []
    for name, entry in peers.items():
        caps = entry.get("capabilities", []) or []
        if capability in caps or capability == "*":
            matches.append((name, entry))
    return matches


def _call_peer_sync(agent_name: str, peer_entry: dict, message: str, context_id: str = "") -> tuple[str, str]:
    """Call a single peer synchronously. Returns (agent_name, reply_text)."""
    try:
        peer = {
            "url": peer_entry.get("url", ""),
            "auth": peer_entry.get("auth", {}) or {},
            "timeout": int(peer_entry.get("timeout", _DEFAULT_TIMEOUT)),
        }
        reply, _ctx, _state = _send_task(agent_name, peer, message, context_id)
        return (agent_name, reply or "(no reply)")
    except Exception as e:
        return (agent_name, f"Error: {e}")


def a2a_orchestrate(args: dict, **_: Any) -> str:
    """Fan-out a task to multiple peer agents by capability.

    Modes:
      - ``all``: send to all peers matching the capability, return all replies.
      - ``first``: send to all matching peers, return the first successful reply.
      - ``best``: send to all, return the longest successful reply (a coarse
        detail heuristic — use ``all`` when you want to judge yourself).

    Configured peers advertise capabilities in config.yaml::

      a2a_agents:
        researcher:
          url: "http://localhost:9991"
          capabilities: [web_search, research]
        coder:
          url: "http://localhost:9992"
          capabilities: [code, debug]
    """
    capability = str(args.get("capability") or "").strip()
    message = str(args.get("message") or args.get("task") or "").strip()
    mode = str(args.get("mode") or "all").strip().lower()
    context_id = str(args.get("context_id") or "").strip()

    if not message:
        return "Error: 'message' is required."
    if not capability:
        return "Error: 'capability' is required (or use '*' for all peers)."

    matches = _match_peers_by_capability(capability)
    if not matches:
        return f"Error: no configured peers advertise capability '{capability}'."

    if mode not in ("all", "first", "best"):
        mode = "all"

    # Fan-out
    results: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(len(matches), _ORCHESTRATE_MAX_WORKERS)) as pool:
        futures = {
            pool.submit(_call_peer_sync, name, entry, message, context_id): name
            for name, entry in matches
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
                if mode == "first" and not results[-1][1].startswith("Error:"):
                    # Got a good reply; cancel peers that haven't started yet.
                    for f in futures:
                        f.cancel()
                    break
            except Exception as e:
                results.append((name, f"Error: {e}"))

    # Sort results by peer name for deterministic output
    results.sort(key=lambda r: r[0])
    successes = [(name, reply) for name, reply in results if not reply.startswith("Error:")]

    def _all_failed() -> str:
        lines = ["All peers failed:"]
        for name, reply in results:
            lines.append(f"  {name}: {reply}")
        return "\n".join(lines)

    if mode == "best":
        if not successes:
            return _all_failed()
        best = max(successes, key=lambda r: len(r[1]))
        return f"[best: {best[0]}]\n{best[1]}"
    elif mode == "first":
        if not successes:
            return _all_failed()
        name, reply = successes[0]
        return f"[first: {name}]\n{reply}"
    else:  # mode == "all"
        lines = [f"Orchestrated '{capability}' to {len(matches)} peer(s):"]
        for name, reply in results:
            lines.append(f"\n--- {name} ---")
            lines.append(reply)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Tool schemas + registration
# --------------------------------------------------------------------------

_FunctionSchema = TypedDict("_FunctionSchema", {"name": str, "description": str, "parameters": dict[str, Any]}, total=False)
_ToolSchema = TypedDict("_ToolSchema", {"type": str, "function": _FunctionSchema}, total=False)
_SCHEMAS: dict[str, _ToolSchema] = {
    "a2a_discover": {
        "type": "function",
        "function": {
            "name": "a2a_discover",
            "description": (
                "Fetch and summarize another agent's A2A Agent Card from a URL "
                "(its name, description, capabilities, and skills). Use this to "
                "find out what a remote agent can do before calling it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Base URL of the remote A2A agent, e.g. http://localhost:9999"},
                },
                "required": ["url"],
            },
        },
    },
    "a2a_call": {
        "type": "function",
        "function": {
            "name": "a2a_call",
            "description": (
                "Send a natural-language task to a remote A2A agent and return "
                "its reply. The agent is a peer (any A2A-compliant framework), "
                "not a sub-agent you control. Pass 'context_id' from a previous "
                "reply to continue a multi-turn exchange."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Configured peer name (from a2a_agents) or a full http(s):// URL."},
                    "message": {"type": "string", "description": "The task / message to send the peer, in natural language."},
                    "context_id": {"type": "string", "description": "Optional: context id from a prior reply, to continue the conversation."},
                },
                "required": ["agent", "message"],
            },
        },
    },
    "a2a_result": {
        "type": "function",
        "function": {
            "name": "a2a_result",
            "description": (
                "Collect the result of a task you started earlier with a2a_call. "
                "Use this when a2a_call reported the peer was still running and "
                "gave you a task id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Configured peer name (from a2a_agents) or a full http(s):// URL."},
                    "task_id": {"type": "string", "description": "Task id reported by a2a_call."},
                },
                "required": ["agent", "task_id"],
            },
        },
    },
    "a2a_list": {
        "type": "function",
        "function": {
            "name": "a2a_list",
            "description": "List configured A2A peer agents, persisted A2A conversations, and metrics.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "a2a_history": {
        "type": "function",
        "function": {
            "name": "a2a_history",
            "description": (
                "Recall a persisted A2A conversation transcript by context_id "
                "(survives restarts and context compaction). Use a2a_list to "
                "see known context ids."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context_id": {"type": "string", "description": "Context id of the conversation to recall."},
                    "limit": {"type": "integer", "description": "Max messages to return (default 50, max 200)."},
                },
                "required": ["context_id"],
            },
        },
    },
    "a2a_orchestrate": {
        "type": "function",
        "function": {
            "name": "a2a_orchestrate",
            "description": (
                "Fan-out a task to multiple peer agents by capability. Peers are "
                "matched from config.yaml a2a_agents.*.capabilities. Modes: 'all' "
                "(return all replies), 'first' (first successful), 'best' (longest "
                "successful reply)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "description": "Capability to match (e.g. 'research', 'code') or '*' for all peers."},
                    "message": {"type": "string", "description": "The task to send to all matching peers."},
                    "mode": {"type": "string", "enum": ["all", "first", "best"], "description": "How to aggregate results. Default: 'all'."},
                    "context_id": {"type": "string", "description": "Optional: shared context id for all peers."},
                },
                "required": ["capability", "message"],
            },
        },
    },
}

_HANDLERS = {
    "a2a_discover": a2a_discover,
    "a2a_call": a2a_call,
    "a2a_result": a2a_result,
    "a2a_list": a2a_list,
    "a2a_history": a2a_history,
    "a2a_orchestrate": a2a_orchestrate,
}


def register_tools(ctx) -> None:
    """Register the client tools in the ``a2a`` toolset."""
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="a2a",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji="\U0001f9e9",  # puzzle piece
        )
