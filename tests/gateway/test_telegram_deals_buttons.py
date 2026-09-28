"""SEC-71: the secret-deals candidate card buttons, ``sd:approve:<id>`` / ``sd:skip:<id>``."""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import unauthorized_action_notice

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import PlatformConfig
from plugins.platforms.telegram import deals_callbacks as sd
from plugins.platforms.telegram.adapter import TelegramAdapter

OWNER = "77770367"
SETTINGS = {"worker_python": "/w/.venv/bin/python", "worker_dir": "/w", "allowed_user_id": int(OWNER)}
CARD_HTML = "<b>Candidate 5</b>\n┌─ title\nA &amp; B\n└─"
CARD_ID = 4321  # the card message the buttons are on


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _query(data, user_id=OWNER):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = -100123
    query.message.message_id = CARD_ID
    query.message.chat.type = "supergroup"
    query.message.message_thread_id = None
    query.message.text_html = CARD_HTML
    query.from_user = MagicMock()
    query.from_user.id = int(user_id)
    query.from_user.first_name = "Jonathan"
    return query


async def _tap(query, *, settings=SETTINGS, answer=sd.WorkerAnswer(True, "approved"), allowed_users="*"):
    update = MagicMock()
    update.callback_query = query
    run = AsyncMock(return_value=answer)
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": allowed_users}), \
            patch.object(sd, "deals_settings", return_value=settings), patch.object(sd, "run_worker", run):
        await _adapter()._handle_callback_query(update, MagicMock())
    return run


def _answer_text(query):
    query.answer.assert_called_once()
    return query.answer.call_args.kwargs["text"]


class TestRouting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("data, verb", [("sd:approve:5", "approve"), ("sd:skip:5", "skip")])
    async def test_sd_callback_runs_the_worker_verb(self, data, verb):
        run = await _tap(_query(data), answer=sd.WorkerAnswer(True, "approved" if verb == "approve" else "skipped"))
        # SEC-44: an approval names the card the tapped button is on; a skip is bound to none
        run.assert_awaited_once_with(verb, 5, SETTINGS, card=CARD_ID if verb == "approve" else None)

    @pytest.mark.asyncio
    async def test_an_approve_tap_with_no_card_message_id_runs_nothing(self):
        query = _query("sd:approve:5")
        query.message.message_id = None
        run = await _tap(query)
        run.assert_not_awaited()
        assert "Nothing was changed" in _answer_text(query)

    @pytest.mark.asyncio
    async def test_other_prefixes_do_not_reach_the_worker(self):
        query = _query("gt:archive:abc")
        with patch("hermes_constants.get_hermes_home", return_value=Path("/nonexistent")):
            run = await _tap(query)
        run.assert_not_awaited()


class TestMalformed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", [
        "sd:", "sd:approve", "sd:approve:", "sd:approve:abc", "sd:approve:5:6", "sd:approve:-5",
        "sd:publish:5", "sd:APPROVE:5", "sd:approve:5\n", "sd:approve: 5", "sd:approve:٥"])
    async def test_malformed_is_answered_and_nothing_runs(self, data):
        query = _query(data)
        run = await _tap(query)
        run.assert_not_awaited()
        assert "Invalid" in _answer_text(query)
        query.edit_message_text.assert_not_called()

    @pytest.mark.parametrize("data, want", [
        ("sd:approve:5", ("approve", 5)), ("sd:skip:0012", ("skip", 12)), ("sd:skip:5x", None), ("", None)])
    def test_parse_callback(self, data, want):
        assert sd.parse_callback(data) == want


class TestAuth:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self):
        query = _query("sd:approve:5", user_id="222")
        run = await _tap(query)  # the gateway allowlist admits everyone; the owner gate does not
        run.assert_not_awaited()
        assert _answer_text(query) == unauthorized_action_notice("telegram")
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_outside_the_gateway_allowlist_is_refused(self):
        query = _query("sd:approve:5")
        run = await _tap(query, allowed_users="111")
        run.assert_not_awaited()
        assert _answer_text(query) == unauthorized_action_notice("telegram")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allowed", [None, "", True])
    async def test_unset_owner_refuses_everyone(self, allowed):
        query = _query("sd:skip:5")
        run = await _tap(query, settings={**SETTINGS, "allowed_user_id": allowed})
        run.assert_not_awaited()
        assert "allowed_user_id is not set" in _answer_text(query)


class TestOutcome:
    @pytest.mark.asyncio
    async def test_cli_refusal_is_answered_and_buttons_stay(self):
        """A double tap: the worker's own answer is shown, the card is not edited."""
        query = _query("sd:approve:5")
        await _tap(query, answer=sd.WorkerAnswer(False, "cannot approve candidate 5: status is approved"))
        assert _answer_text(query) == "cannot approve candidate 5: status is approved"
        assert query.answer.call_args.kwargs["show_alert"] is True
        query.edit_message_text.assert_not_called()
        query.edit_message_reply_markup.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_error_is_cut_to_the_bot_api_cap(self):
        query = _query("sd:approve:5")
        await _tap(query, answer=sd.WorkerAnswer(False, "x" * 500))
        assert len(_answer_text(query)) == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data, status, label", [
        ("sd:approve:5", "approved", "✅ Approved"), ("sd:skip:5", "skipped", "❌ Skipped")])
    async def test_success_strips_buttons_and_appends_status(self, data, status, label):
        query = _query(data)
        await _tap(query, answer=sd.WorkerAnswer(True, status))
        assert _answer_text(query) == label
        kwargs = query.edit_message_text.call_args.kwargs
        assert kwargs["reply_markup"] is None
        assert "HTML" in repr(kwargs["parse_mode"])
        assert kwargs["text"] == f"{CARD_HTML}\n\n— {label} by Jonathan"

    @pytest.mark.asyncio
    async def test_failed_text_edit_still_strips_buttons(self):
        query = _query("sd:approve:5")
        query.edit_message_text.side_effect = RuntimeError("message is too long")
        await _tap(query)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def _fake_worker(tmp_path, body):
    """A stand-in ``<venv>/bin/secret-deals`` that records argv, cwd and env, then runs *body*."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "secret-deals"
    script.write_text(f"#!/bin/sh\necho \"$@\" > \"$PWD/argv\"\nenv > \"$PWD/env\"\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    workdir = tmp_path / "worker"
    workdir.mkdir()
    return {"worker_python": str(bin_dir / "python"), "worker_dir": str(workdir), "allowed_user_id": OWNER}, workdir


class TestRunWorker:
    @pytest.mark.asyncio
    async def test_success_runs_the_console_script_with_json_in_the_worker_dir(self, tmp_path):
        settings, workdir = _fake_worker(tmp_path, "echo '{\"ok\": true, \"id\": 5, \"status\": \"approved\"}'")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret-canary"}):
            answer = await sd.run_worker("approve", 5, settings)
        assert answer == sd.WorkerAnswer(True, "approved")
        assert (workdir / "argv").read_text().strip() == "approve 5 --json"
        assert "secret-canary" not in (workdir / "env").read_text()

    @pytest.mark.asyncio
    async def test_refusal_returns_the_workers_detail(self, tmp_path):
        payload = {"ok": False, "error": "illegal_transition", "detail": "cannot skip candidate 5: posted",
                   "id": 5, "status": "posted"}
        settings, _ = _fake_worker(tmp_path, f"echo '{json.dumps(payload)}'\nexit 2")
        assert await sd.run_worker("skip", 5, settings) == sd.WorkerAnswer(False, "cannot skip candidate 5: posted")

    @pytest.mark.asyncio
    async def test_an_approval_passes_the_card_it_answers(self, tmp_path):
        settings, workdir = _fake_worker(tmp_path, "echo '{\"ok\": true, \"id\": 5, \"status\": \"approved\"}'")
        assert await sd.run_worker("approve", 5, settings, card=CARD_ID) == sd.WorkerAnswer(True, "approved")
        assert (workdir / "argv").read_text().strip() == f"approve 5 --card={CARD_ID} --json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current, needle", [(4400, "The newer card is message 4400"), (None, "no current card")])
    async def test_a_stale_card_is_answered_with_where_the_newer_card_is(self, tmp_path, current, needle):
        payload = {"ok": False, "error": "stale_card", "detail": "card 4321 is not the current card",
                   "id": 5, "status": "pending", "card_message_id": current}
        settings, _ = _fake_worker(tmp_path, f"echo '{json.dumps(payload)}'\nexit 2")
        answer = await sd.run_worker("approve", 5, settings, card=CARD_ID)
        assert not answer.ok and "out of date" in answer.text and needle in answer.text
        assert len(answer.text) <= 200  # a callback answer's cap

    @pytest.mark.asyncio
    async def test_no_json_is_not_a_refusal_and_keeps_stderr_out(self, tmp_path):
        settings, _ = _fake_worker(tmp_path, "echo 'Traceback: seller title' >&2\nexit 1")
        answer = await sd.run_worker("approve", 5, settings)
        assert not answer.ok and "no answer (exit 1)" in answer.text and "seller" not in answer.text

    @pytest.mark.asyncio
    async def test_timeout_says_outcome_unknown(self, tmp_path):
        settings, _ = _fake_worker(tmp_path, "exec sleep 5")
        answer = await sd.run_worker("approve", 5, settings, timeout=0.3)
        assert not answer.ok and "unknown" in answer.text

    @pytest.mark.asyncio
    async def test_unconfigured(self):
        answer = await sd.run_worker("approve", 5, {"allowed_user_id": OWNER})
        assert not answer.ok and "not configured" in answer.text

    def test_settings_come_from_the_deals_plugin_entry(self):
        cfg = {"plugins": {"entries": {"deals": {"settings": SETTINGS}}}}
        with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            assert sd.deals_settings() == SETTINGS
        with patch("hermes_cli.config.load_config_readonly", return_value={}):
            assert sd.deals_settings() == {}
