"""secret-deals card buttons (SEC-71, SEC-102, SEC-125): ``sd:approve:<id>``, ``sd:approve_both:<id>``,
``sd:skip:<id>``, and the taste-card verbs ``sd:taste_post:<id>``, ``sd:taste_never:<id>``, ``sd:taste_unsure:<id>``.

The secret-deals worker posts each candidate card to the ops group as this bot. Since SEC-150 a card
has one ✅ Approve (``sd:approve_both``: approves with the worker's ``--placement both``, so a Facebook
version is prepared for Jonathan to post by hand) and ❌ (``sd:skip``); ``sd:approve`` stays for older
cards and now gets the worker's default, which is ``both`` too. A tap runs the worker's own CLI the way the ``deals`` plugin (SEC-14) does: the venv's
``bin/secret-deals <verb> <id> --json``, the worker dir as cwd, a bare environment, a hard timeout.
The worker owns every rule (which status may be approved or skipped); this module only parses the
button, runs the CLI and reads back the one JSON object it prints (contract: the secret-deals
repo's ``docs/cawl-contract.md``). Never the SQLite store directly.

SEC-125: the daily taste batch posts one-photo cards to the Ops group's Taste topic with 👍 / 👎 / 🤷. Those
three verbs carry a taste *ask* id (not a candidate id) and run the worker's ``taste answer <ask_id>
post|never|unsure --json``. The worker owns what an answer does; this module only maps the verb and reads
back the one JSON object. A taste tap never answers in a channel: Cawl only edits the card it was tapped on.

Settings are the deals plugin's own, ``plugins.entries.deals.settings``: ``worker_python``,
``worker_dir`` and ``allowed_user_id``, so the buttons and the tools share one worker and one
owner. Self-contained on purpose: the plugin may be installed under ``~/.hermes/plugins/deals``
rather than bundled, so its helpers are not importable from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "sd:"
PLUGIN_ID = "deals"
TIMEOUT_SECONDS = 30  # the deals plugin's approve/skip timeout
_STDERR_LOG_CHARS = 2000

# [0-9], not \d: \d also matches non-ASCII digits. fullmatch, not $: $ allows a trailing newline.
_DATA_RE = re.compile(r"sd:(approve|approve_both|skip|taste_post|taste_never|taste_unsure):([0-9]+)")

# SEC-125: the taste verbs and the worker answer each one records (``taste answer <ask_id> <answer>``).
TASTE_ANSWERS = {"taste_post": "post", "taste_never": "never", "taste_unsure": "unsure"}


@dataclass(frozen=True)
class WorkerAnswer:
    """``ok`` is the worker's own ``ok``; ``text`` is its ``status`` on success, else what to show."""

    ok: bool
    text: str


def parse_callback(data: str) -> Optional[tuple[str, int]]:
    """``(verb, id)`` for exactly ``sd:<verb>:<digits>`` with verb one of ``approve``, ``approve_both``,
    ``skip`` (a candidate id) or ``taste_post``, ``taste_never``, ``taste_unsure`` (a taste ask id), else None."""
    match = _DATA_RE.fullmatch(data or "")
    return (match.group(1), int(match.group(2))) if match else None


def deals_settings() -> Mapping[str, Any]:
    """``plugins.entries.deals.settings`` from the live config, or ``{}``."""
    from hermes_cli.config import load_config_readonly

    entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
    entry = entries.get(PLUGIN_ID) if isinstance(entries, Mapping) else None
    settings = entry.get("settings") if isinstance(entry, Mapping) else None
    return settings if isinstance(settings, Mapping) else {}


def owner_id(settings: Mapping[str, Any]) -> str:
    """The configured ``allowed_user_id`` as a string; ``""`` when unset (then nobody may act)."""
    allowed = settings.get("allowed_user_id")
    return str(allowed).strip() if allowed is not None and not isinstance(allowed, bool) else ""


def _child_env() -> dict:
    """Bare environment, as the launchd jobs and the deals plugin run the worker: Hermes's own
    secrets stay out of it, and a stray ``SECRET_DEALS_*`` cannot redirect its store."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ") if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _stale_card_text(candidate_id: int, current: Any) -> str:
    """SEC-44's ``stale_card``, relayed: where the newer card is. Fixed text plus ids; fits the 200-char
    callback answer."""
    if isinstance(current, int) and not isinstance(current, bool):
        return (f"That card is out of date. The newer card is message {current} (candidate {candidate_id}): "
                "approve from that one. Nothing was changed.")
    return (f"That card is out of date, and candidate {candidate_id} has no current card right now. "
            "Nothing was changed.")


async def run_worker(verb: str, candidate_id: int, settings: Mapping[str, Any],
                     *, card: Optional[int] = None, timeout: float = TIMEOUT_SECONDS) -> WorkerAnswer:
    """Run ``secret-deals <verb> <candidate_id> [--card=<card>] --json`` and read its answer.

    Verb ``approve_both`` (SEC-102) runs the worker's ``approve <id> --placement both --card=<card> --json``.
    A taste verb (SEC-125) runs ``taste answer <id> post|never|unsure --json``: the id is a taste ask's, and
    there is no ``--card``, because a tap names its ask and the last tap wins.
    ``card`` (SEC-44) is the message the tapped button is on: an approval applies only while that is
    still the candidate's current card. A ``stale_card`` refusal comes back as fixed text naming the
    newer card; any other refusal (``ok: false``) as the worker's own ``detail``, which the contract builds
    from ids, statuses and code text only, never seller text. No JSON, a timeout or a failed start
    comes back as a fixed message; stderr goes to the log only."""
    python, workdir = settings.get("worker_python"), settings.get("worker_dir")
    if not python or not workdir:
        return WorkerAnswer(False, "Deals buttons are not configured: set plugins.entries.deals.settings."
                                   "worker_python and worker_dir. Nothing was changed.")
    script = Path(str(python)).parent / "secret-deals"  # the venv's console script, as launchd runs it
    taste = verb in TASTE_ANSWERS
    if taste:
        argv = ["taste", "answer", str(candidate_id), TASTE_ANSWERS[verb], "--json"]
    else:
        command, placement = ("approve", ["--placement", "both"]) if verb == "approve_both" else (verb, [])
        argv = [command, str(candidate_id), *placement, *([] if card is None else [f"--card={card}"]), "--json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            str(script), *argv,
            cwd=str(workdir), env=_child_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        logger.warning("deals button: cannot start worker %s: %s", script, exc)
        return WorkerAnswer(False, f"Could not start the worker ({type(exc).__name__}). Nothing was changed.")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("deals button: worker %s %s timed out after %ss", verb, candidate_id, timeout)
        subject = f"taste card {candidate_id} was answered" if taste else f"candidate {candidate_id} changed"
        return WorkerAnswer(False, f"The worker did not answer within {timeout:g}s. Whether {subject} is unknown: "
                                   + ("tap again to be sure." if taste else "ask Cawl for its status before retrying."))
    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(stdout) if stdout else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        logger.warning("deals button: worker %s %s exit %s gave no JSON answer; stderr tail: %s",
                       verb, candidate_id, proc.returncode, stderr[-_STDERR_LOG_CHARS:])
        return WorkerAnswer(False, f"The worker gave no answer (exit {proc.returncode}). Details are in the "
                                   "gateway log.")
    if payload["ok"]:
        return WorkerAnswer(True, str(payload.get("status") or ""))
    if payload.get("error") == "stale_card":
        return WorkerAnswer(False, _stale_card_text(candidate_id, payload.get("card_message_id")))
    detail = str(payload.get("detail") or "").strip()
    return WorkerAnswer(False, detail or f"The worker refused ({payload.get('error') or 'no error code'}).")
