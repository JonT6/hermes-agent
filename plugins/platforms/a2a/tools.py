"""A2A client tools (``a2a`` toolset): a2a_discover/call/result/list/history/orchestrate talk to *other*
agents. Peers come from config.yaml ``a2a_agents: {name: {url, auth: {type: bearer, token}, timeout,
poll_timeout, capabilities}}``. Stdlib urllib; wire format is A2A v1.0 ``SendMessage`` (v0.3 replies still parse)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from gateway.platforms._shared import coerce_port as _coerce_int, get_scoped_secret as _get_scoped_secret

from . import protocol, security

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120

# How long we wait for a peer to FINISH, as opposed to how long one HTTP request may take
# (`timeout`). Conflating the two made a2a_call fire-and-forget (AIA-12): a peer that correctly
# returns `working` at once — leo-a2a does, a triage takes minutes — was treated as having answered.
# 300s is a compromise, not a law: leo-a2a's own wall clock is 900s, so a long triage takes the loud
# "still running" path with a task id for a2a_result. Waiting 900 would pin an operator's Telegram
# turn for fifteen minutes; waiting 120 would abandon most real work. Raise it per peer with
# `poll_timeout` where nobody is waiting, e.g. cron.
_DEFAULT_POLL_TIMEOUT_S = 300
_POLL_INTERVAL_S = 3

# States where the peer has stopped talking *for now*. Deliberately NOT protocol.TERMINAL_STATES:
# input-required and auth-required are terminal FOR THE CALLER even though the task is unfinished —
# polling them would wait forever on input only we can supply. The auth-required literal is spelled
# out because protocol only exports it through the revert-scheduled PLUGIN-COMPAT block.
_TERMINAL_STATES = protocol.TERMINAL_STATES | {protocol.STATE_INPUT_REQUIRED, "TASK_STATE_AUTH_REQUIRED"}
_ORCHESTRATE_MAX_WORKERS = 6  # max parallel peers for fan-out


def _load_config() -> dict:
    """Read-only view of config.yaml; peers are only read, never mutated (cache-safe)."""
    from hermes_cli.config import load_config_readonly
    return load_config_readonly() or {}


def _configured_peers() -> dict:
    return _load_config().get("a2a_agents") or {}


def _peer_from_entry(entry: dict, **extra: Any) -> dict:
    return {"url": entry.get("url", ""), "auth": entry.get("auth", {}) or {},
            "timeout": int(entry.get("timeout", _DEFAULT_TIMEOUT)),
            "poll_timeout": float(entry.get("poll_timeout", _DEFAULT_POLL_TIMEOUT_S)), **extra}


def _resolve_peer(agent: str) -> Optional[dict]:
    """Peer name -> {url, auth, timeout, poll_timeout, capabilities, tenant}, or treat ``agent`` as a URL."""
    if agent.startswith(("http://", "https://")):
        return {"url": agent, "auth": {}, "timeout": _DEFAULT_TIMEOUT,
                "poll_timeout": _DEFAULT_POLL_TIMEOUT_S, "capabilities": []}
    entry = _configured_peers().get(agent)
    return _peer_from_entry(entry, capabilities=entry.get("capabilities", []) or [], tenant=entry.get("tenant", "")) if entry else None


def _auth_header(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['token']}"} if auth and auth.get("type") == "bearer" and auth.get("token") else {}


def _http_json(url: str, headers: dict, timeout: int, method: str, data: Optional[bytes] = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str, headers: dict, timeout: int) -> dict:
    return _http_json(url, headers, timeout, "GET")


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    hdrs = {"Content-Type": "application/json", "A2A-Version": protocol.PROTOCOL_VERSION, **headers}
    return _http_json(url, hdrs, timeout, "POST", json.dumps(body).encode("utf-8"))


def _fetch_card(base_url: str, headers: dict, timeout: int) -> dict:
    """GET the v1.0 agent-card.json; on 404 fall back to the v0.2 agent.json alias."""
    base = base_url.rstrip("/")
    try:
        return _http_get_json(base + "/.well-known/agent-card.json", headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return _http_get_json(base + "/.well-known/agent.json", headers, timeout)


def _select_jsonrpc_interface(card: Optional[dict]) -> Optional[dict]:
    if isinstance(card, dict):
        for iface in card.get("supportedInterfaces", []) or []:
            if isinstance(iface, dict) and iface.get("protocolBinding") == "JSONRPC" and iface.get("url"):
                return iface
    return None


def _rpc_url(base_url: str, card: Optional[dict]) -> str:
    """Card's JSONRPC interface (v1.0 supportedInterfaces) > card's legacy top-level url > base."""
    if iface := _select_jsonrpc_interface(card):
        return str(iface["url"])
    if isinstance(card, dict) and isinstance(card.get("url"), str) and card["url"]:
        return card["url"]
    return base_url.rstrip("/")


def _short_state(state: str) -> str:
    return state.replace("TASK_STATE_", "").replace("_", "-").lower() if state else ""  # v0.3 states pass through


def _get_task_body(task_id: str) -> dict:
    return {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": "GetTask", "params": {"id": task_id}}


def _poll_until_terminal(rpc_url: str, headers: dict, task_id: str,
                         timeout: int, budget: float) -> tuple[Optional[dict], str]:
    """Poll ``GetTask`` until the task stops moving or the budget runs out -> (payload, state).

    A payload of ``None`` means the budget expired with the task still in flight. That is NOT a
    failure and must not be rendered as an empty reply: the result is still collectable by task id.
    A transport blip mid-poll is skipped rather than read as a failed task — the work runs on the
    peer whether or not one status request made it there — and the deadline bounds the retries."""
    deadline = time.monotonic() + budget
    state = protocol.STATE_WORKING
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        try:
            resp = _http_post_json(rpc_url, _get_task_body(task_id), headers, timeout)
        except (OSError, ValueError) as e:  # URLError/HTTPError/timeouts are OSError; bad JSON is ValueError
            logger.debug("A2A: GetTask %s poll failed, retrying: %s", task_id, e)
            continue
        if "error" in resp:
            logger.debug("A2A: GetTask %s returned an error, retrying: %s", task_id, resp["error"])
            continue
        payload = protocol.unwrap_send_message_response(resp.get("result", {}))
        if isinstance(payload, dict):
            state = (payload.get("status") or {}).get("state", state)
            if state in _TERMINAL_STATES:
                return payload, state
    return None, state


def _send_task(agent_label: str, peer: dict, message: str, context_id: str) -> tuple[str, str, str]:
    """One SendMessage to a peer, polled to a terminal state -> (reply_text, context_id, state).
    Raises urllib errors / ValueError for the caller to format; handles redaction, audit,
    persistence, metrics."""
    base_url = peer.get("url", "")
    headers = _auth_header(peer.get("auth", {}) or {})
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30))  # best-effort, to learn the rpc URL
    except Exception:
        card = None
    ctx = context_id or protocol.new_context_id()
    safe_message = security.redact_outbound(message)
    # v1.0: contextId lives inside the Message, not at the params top level.
    rpc_body = {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": "SendMessage",
                "params": {"message": protocol.text_message(protocol.ROLE_USER, safe_message, context_id=ctx)}}
    iface = _select_jsonrpc_interface(card)
    tenant = str(iface["tenant"]) if iface and iface.get("tenant") else str(peer.get("tenant") or "")
    if tenant:
        rpc_body["params"]["tenant"] = tenant
    # AIA-19 / D2: nothing is recorded as sent until the wire says so. The audit write used to run
    # BEFORE the POST, so a message that never left (wrong bearer, peer down) still wrote an
    # "outbound" row. And a 200 only means the peer's SERVER accepted it — on 2026-08-13 a 200 with a
    # real task id failed seven seconds later — so the row written here is `relay.queued`, and only
    # the terminal-state check below may write `relay.delivered`.
    rpc_url = _rpc_url(base_url, card)
    try:
        resp = _http_post_json(rpc_url, rpc_body, headers, timeout)
    except Exception as exc:
        code = getattr(exc, "code", None)
        security.audit("outbound", agent_label, rpc_body["id"],
                       f"transport failed before delivery: {type(exc).__name__}" + (f" HTTP {code}" if code else ""),
                       outcome="relay.failed")
        raise
    if "error" in resp:
        err = resp["error"]
        security.audit("outbound", agent_label, rpc_body["id"],
                       f"peer returned a JSON-RPC error: {err.get('message', err)}", outcome="relay.failed")
        raise ValueError(f"Peer '{agent_label}' returned an error: {err.get('message', err)}")
    security.audit("outbound", agent_label, rpc_body["id"], safe_message, outcome="relay.queued")
    protocol.persist_message(ctx, "user", safe_message, rpc_body["id"])
    protocol.metrics.outbound_total += 1
    payload = protocol.unwrap_send_message_response(resp.get("result", {}))
    reply_ctx, state, task_id = ctx, "", ""
    if isinstance(payload, dict):
        reply_ctx = payload.get("contextId", ctx)
        state = (payload.get("status") or {}).get("state", "")
        task_id = payload.get("id", "")
    # AIA-12: collect the ANSWER, not the acknowledgement. A peer that returns `working` has told us
    # nothing yet; returning here is what made this tool fire-and-forget.
    if task_id and state and state not in _TERMINAL_STATES:
        budget = float(peer.get("poll_timeout", _DEFAULT_POLL_TIMEOUT_S))
        polled, state = _poll_until_terminal(rpc_url, headers, task_id, timeout, budget)
        if polled is None:
            # Loud and actionable: the old "(no text reply)" read as "nothing to say", not "still working".
            reply = (f"Still running after {budget:g}s — '{agent_label}' has not finished task {task_id}. "
                     f"This is not a failure: the peer is still working. Collect the result with "
                     f"a2a_result(agent='{agent_label}', task_id='{task_id}').")
            protocol.persist_message(reply_ctx, "agent", reply, rpc_body["id"])
            protocol.metrics.inbound_total += 1
            return reply, reply_ctx, state
        payload = polled
        reply_ctx = payload.get("contextId", reply_ctx)
    reply = _reply_text_from_result(payload)
    # AIA-19 / D2: the ONLY place allowed to say "delivered", because it is the only place that has
    # seen a terminal state. A task that terminated FAILED is recorded as such even though transport
    # succeeded — conflating the two sent an operator after a phantom auth problem on 2026-08-13.
    # The `relay.queued` row stays; this one supersedes it, so the pair reads as a history.
    security.audit("outbound", agent_label, task_id or rpc_body["id"],
                   f"peer task terminal in state {_short_state(state) or 'unknown'}",
                   outcome="relay.failed" if state == protocol.STATE_FAILED else "relay.delivered")
    protocol.persist_message(reply_ctx, "agent", reply, rpc_body["id"])
    protocol.metrics.inbound_total += 1
    return reply, reply_ctx, state


def _reply_text_from_result(result: Any) -> str:
    result = protocol.unwrap_send_message_response(result)
    if not isinstance(result, dict):
        return str(result)
    # Artifacts first (final output), then status message (interim/clarify), else bare Message.
    for artifact in result.get("artifacts", []) or []:
        txt = protocol.extract_text(artifact)
        if txt:
            return txt
    return protocol.extract_text((result.get("status", {}) or {}).get("message") or result)


_AUTH_ERR = "Error: peer '{agent}' rejected auth (HTTP {code}). Check the configured token."
_HTTP_CALL_ERRORS = {401: _AUTH_ERR, 403: _AUTH_ERR, 429: "Error: peer '{agent}' rate limited us (HTTP 429). Retry later."}

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
    caps = card.get("capabilities", {}) or {}
    skills = card.get("skills", []) or []
    auth = "yes" if card.get("security") else "no"
    proto = ", ".join(
        f"{i.get('protocolBinding', '?')} v{i.get('protocolVersion', '?')}"
        for i in (card.get("supportedInterfaces", []) or []) if isinstance(i, dict)
    ) or f"v{card.get('protocolVersion', '?')} (pre-1.0 card)"
    lines = [f"Agent: {card.get('name', '?')}", f"Description: {card.get('description', '')}", f"URL: {_rpc_url(url, card)}",
             f"Protocol: {proto}",
             f"Streaming: {bool(caps.get('streaming'))}  Push: {bool(caps.get('pushNotifications'))}  Auth required: {auth}",
             f"Skills ({len(skills)}):"]
    lines.extend(f"  - {s.get('name', s.get('id', '?'))}: {s.get('description', '')}" for s in skills[:20])
    return "\n".join(lines)


def a2a_call(args: dict, **_: Any) -> str:
    """Send a task to a peer (configured name or direct URL); ``context_id`` continues a prior exchange.
    Blocks until the peer finishes (polling ``GetTask`` when it accepts asynchronously); past
    ``poll_timeout`` it says so and hands back the task id for ``a2a_result`` — never a silent empty reply."""
    # Accept common aliases models reach for (observed live: 'agent_name').
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not agent or not message:
        return "Error: both 'agent' and 'message' are required."
    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in config.yaml or pass a full http(s):// URL."
    try:
        reply, reply_ctx, state = _send_task(agent, peer, message, context_id)
    except urllib.error.HTTPError as e:
        return _HTTP_CALL_ERRORS.get(e.code, "Error: call to '{agent}' failed — HTTP {code}.").format(agent=agent, code=e.code)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: call to '{agent}' failed — {e}."
    header = f"[{agent} · context {reply_ctx}" + (f" · {_short_state(state)}" if state else "") + "]"
    body = reply or "(no text reply)"
    if state == protocol.STATE_INPUT_REQUIRED:
        body += f"\n\n(The peer needs more input — answer by calling a2a_call again with context_id '{reply_ctx}'.)"
    if state == protocol.STATE_FAILED:
        # AIA-19 / D1: everything above already succeeded — request delivered, bearer accepted, task
        # created. The peer's own processing failed, and its error text on 2026-08-13 ("Failed to
        # authenticate. API Error: 401 …") was indistinguishable from OUR auth failure (_AUTH_ERR).
        # An operator spent ~30 minutes checking a bearer that worked. "failed" never says WHOSE, so
        # say it in words.
        body = ("⚠️ This is the PEER's failure, not ours.\n"
                f"Our request reached '{agent}' and was accepted — transport and auth to the peer are fine. "
                "The peer's own task then failed, and the text below is ITS error, reported verbatim. Nothing "
                "here indicates a problem with our bearer token or configuration; look at the peer's side.\n\n"
                f"{body}")
    return f"{header}\n{body}"


def a2a_result(args: dict, **_: Any) -> str:
    """Collect a task started earlier by ``a2a_call`` — the escape hatch for work that outlived
    ``poll_timeout``: a2a_call hands back a task id, and this fetches it whenever the caller returns."""
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    task_id = str(args.get("task_id") or args.get("taskId") or args.get("id") or "").strip()
    if not agent or not task_id:
        return "Error: both 'agent' and 'task_id' are required."
    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in config.yaml or pass a full http(s):// URL."
    base_url = peer.get("url", "")
    headers = _auth_header(peer.get("auth", {}) or {})
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30))  # best-effort, to learn the rpc URL
    except Exception:
        card = None
    try:
        resp = _http_post_json(_rpc_url(base_url, card), _get_task_body(task_id), headers, timeout)
    except urllib.error.HTTPError as e:
        return _HTTP_CALL_ERRORS.get(e.code, "Error: call to '{agent}' failed — HTTP {code}.").format(agent=agent, code=e.code)
    except Exception as e:
        return f"Error: call to '{agent}' failed — {e}."
    if "error" in resp:
        return f"Error: peer '{agent}' returned an error: {resp['error'].get('message', resp['error'])}"
    payload = protocol.unwrap_send_message_response(resp.get("result", {}))
    state = (payload.get("status") or {}).get("state", "") if isinstance(payload, dict) else ""
    text = _reply_text_from_result(payload) or "(no text yet — still working)"
    return f"[{agent} · task {task_id} · {_short_state(state)}]\n{text}"


def a2a_list(args: dict | None = None, **_: Any) -> str:
    """List configured A2A peers, persisted conversations, and metrics."""
    peers = _configured_peers()
    lines = []
    if peers:
        lines.append(f"Configured peers ({len(peers)}):")
        for name, entry in peers.items():
            caps = entry.get("capabilities", [])
            lines.append(f"  - {name}: {entry.get('url', '?')} (auth: {(entry.get('auth') or {}).get('type', 'none')})"
                         + (f" caps: {', '.join(caps)}" if caps else ""))
    else:
        lines.append("No peers configured. Add them under 'a2a_agents' in config.yaml.")
    if convos := protocol.list_conversations():
        lines.append("")
        lines.append(f"Persisted conversations ({len(convos)}) — recall with a2a_history:")
        lines.extend(f"  - {c}" for c in convos[:25])
    m = protocol.metrics.snapshot()
    lines.append("")
    lines.append(f"Metrics: {m['inbound_total']} in / {m['outbound_total']} out, {m['tasks_completed']} completed, "
                 f"{m['tasks_failed']} failed, {m['streams_started']} streams, {m['push_sent']} push sent, "
                 f"{m['anti_loop_triggers']} anti-loop, {m['rate_limit_triggers']} rate-limited, avg {m['avg_latency_ms']}ms")
    return "\n".join(lines)


def a2a_history(args: dict, **_: Any) -> str:
    """Recall a persisted A2A conversation (survives compaction/restarts)."""
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not context_id:
        return "Error: 'context_id' is required (see a2a_list for known conversations)."
    limit = max(1, min(_coerce_int(args.get("limit") or 50, 50), 200))
    messages = protocol.load_conversation(context_id, limit=limit)
    if not messages:
        return f"No persisted conversation for context '{context_id}'."
    lines = [f"Conversation {context_id} (last {len(messages)} messages):"]
    for m in messages:
        text = (m.get("text") or "").strip()
        lines.append(f"[{m.get('role', '?')}] {text[:1000] + ' …[truncated]' if len(text) > 1000 else text}")
    return "\n".join(lines)


def _match_peers_by_capability(capability: str) -> list[tuple[str, dict]]:
    """Configured peers that advertise the capability ('*' matches all)."""
    return [(name, entry) for name, entry in _configured_peers().items()
            if capability in (entry.get("capabilities", []) or []) or capability == "*"]


def _call_peer_sync(agent_name: str, peer_entry: dict, message: str, context_id: str = "") -> tuple[str, str]:
    """Call a single peer synchronously -> (agent_name, reply_text)."""
    try:
        reply, _ctx, _state = _send_task(agent_name, _peer_from_entry(peer_entry), message, context_id)
        return (agent_name, reply or "(no reply)")
    except Exception as e:
        return (agent_name, f"Error: {e}")


def a2a_orchestrate(args: dict, **_: Any) -> str:
    """Fan-out a task to peers matching a capability. Modes: ``all``, ``first`` (first successful),
    ``best`` (longest successful — coarse; use ``all`` to judge yourself)."""
    capability = str(args.get("capability") or "").strip()
    message = str(args.get("message") or args.get("task") or "").strip()
    mode = str(args.get("mode") or "all").strip().lower()
    mode = mode if mode in ("all", "first", "best") else "all"
    context_id = str(args.get("context_id") or "").strip()
    if not message:
        return "Error: 'message' is required."
    if not capability:
        return "Error: 'capability' is required (or use '*' for all peers)."
    if not (matches := _match_peers_by_capability(capability)):
        return f"Error: no configured peers advertise capability '{capability}'."
    results: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(len(matches), _ORCHESTRATE_MAX_WORKERS)) as pool:
        futures = {pool.submit(_call_peer_sync, name, entry, message, context_id): name for name, entry in matches}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
                if mode == "first" and not results[-1][1].startswith("Error:"):
                    for f in futures:  # good reply; cancel peers that haven't started
                        f.cancel()
                    break
            except Exception as e:
                results.append((name, f"Error: {e}"))
    results.sort(key=lambda r: r[0])  # deterministic output
    successes = [(name, reply) for name, reply in results if not reply.startswith("Error:")]
    if mode in ("best", "first"):
        if not successes:
            return "\n".join(["All peers failed:"] + [f"  {name}: {reply}" for name, reply in results])
        name, reply = max(successes, key=lambda r: len(r[1])) if mode == "best" else successes[0]
        return f"[{mode}: {name}]\n{reply}"
    return "\n".join([f"Orchestrated '{capability}' to {len(matches)} peer(s):"]
                     + [line for name, reply in results for line in (f"\n--- {name} ---", reply)])


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


# name -> (handler, description, properties, required)
_TOOLS: dict[str, tuple[Any, str, dict, list[str]]] = {
    "a2a_discover": (a2a_discover,
                     "Fetch and summarize another agent's A2A Agent Card from a URL (its name, description, "
                     "capabilities, and skills). Use this to find out what a remote agent can do before calling it.",
                     {"url": _str("Base URL of the remote A2A agent, e.g. http://localhost:9999")}, ["url"]),
    "a2a_call": (a2a_call,
                 "Send a natural-language task to a remote A2A agent and return its reply. The agent is a peer "
                 "(any A2A-compliant framework), not a sub-agent you control. Pass 'context_id' from a previous "
                 "reply to continue a multi-turn exchange.",
                 {"agent": _str("Configured peer name (from a2a_agents) or a full http(s):// URL."),
                  "message": _str("The task / message to send the peer, in natural language."),
                  "context_id": _str("Optional: context id from a prior reply, to continue the conversation.")},
                 ["agent", "message"]),
    "a2a_result": (a2a_result,
                   "Collect the result of a task you started earlier with a2a_call. Use this when a2a_call "
                   "reported the peer was still running and gave you a task id.",
                   {"agent": _str("Configured peer name (from a2a_agents) or a full http(s):// URL."),
                    "task_id": _str("Task id reported by a2a_call.")},
                   ["agent", "task_id"]),
    "a2a_list": (a2a_list, "List configured A2A peer agents, persisted A2A conversations, and metrics.", {}, []),
    "a2a_history": (a2a_history,
                    "Recall a persisted A2A conversation transcript by context_id (survives restarts and "
                    "context compaction). Use a2a_list to see known context ids.",
                    {"context_id": _str("Context id of the conversation to recall."),
                     "limit": {"type": "integer", "description": "Max messages to return (default 50, max 200)."}},
                    ["context_id"]),
    "a2a_orchestrate": (a2a_orchestrate,
                        "Fan-out a task to multiple peer agents by capability. Peers are matched from config.yaml "
                        "a2a_agents.*.capabilities. Modes: 'all' (return all replies), 'first' (first successful), "
                        "'best' (longest successful reply).",
                        {"capability": _str("Capability to match (e.g. 'research', 'code') or '*' for all peers."),
                         "message": _str("The task to send to all matching peers."),
                         "mode": {"type": "string", "enum": ["all", "first", "best"], "description": "How to aggregate results. Default: 'all'."},
                         "context_id": _str("Optional: shared context id for all peers.")},
                        ["capability", "message"]),
}


def _a2a_tools_available() -> bool:
    """check_fn: serve the client tools ONLY when the operator opted into A2A (peers under
    ``a2a_agents``, inbound platform enabled, or A2A_PORT set). Fail closed.

    Maintainer-directed (#95681): these registered unconditionally, so every session on every install paid
    ~561 tok/call for tools whose only possible output without config is 'no peers configured'. A2A is
    unrelated to Bot Mode (bots talk over gateway RPCs) — for most installs this toolset is foreign-agent
    plumbing they never enabled. Config adds mid-session surface at the next compaction (#97073).
    """
    cfg = {}
    with contextlib.suppress(Exception):
        cfg = _load_config()
        if cfg.get("a2a_agents"):
            return True
    try:
        # Scoped like the platform gate: os.environ is the launch profile's under multiplexing (#122126).
        if _get_scoped_secret("A2A_PORT"):
            return True
        a2a_cfg = (cfg.get("platforms") or {}).get("a2a") or {}
        return bool(isinstance(a2a_cfg, dict) and a2a_cfg.get("enabled"))
    except Exception:
        return False


def register_tools(ctx) -> None:
    """Register the client tools in the ``a2a`` toolset (config-gated)."""
    for name, (handler, description, properties, required) in _TOOLS.items():
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(name=name, toolset="a2a", handler=handler, description=description,
                          schema={"name": name, "description": description, "parameters": parameters},
                          emoji="\U0001f9e9", check_fn=_a2a_tools_available)  # puzzle piece
