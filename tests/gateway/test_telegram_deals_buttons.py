"""SEC-71: the secret-deals candidate card buttons, ``sd:approve:<id>`` / ``sd:skip:<id>``.

SEC-125 adds the taste-card verbs ``sd:taste_post|taste_never|taste_unsure:<id>`` (classes at the end)."""

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
    @pytest.mark.parametrize("data, verb", [
        ("sd:approve:5", "approve"), ("sd:approve_both:5", "approve_both"), ("sd:skip:5", "skip")])
    async def test_sd_callback_runs_the_worker_verb(self, data, verb):
        run = await _tap(_query(data), answer=sd.WorkerAnswer(True, "skipped" if verb == "skip" else "approved"))
        # SEC-44: either approval names the card the tapped button is on; a skip is bound to none
        run.assert_awaited_once_with(verb, 5, SETTINGS, card=None if verb == "skip" else CARD_ID)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", ["sd:approve:5", "sd:approve_both:5"])
    async def test_an_approve_tap_with_no_card_message_id_runs_nothing(self, data):
        query = _query(data)
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
        "sd:publish:5", "sd:APPROVE:5", "sd:approve:5\n", "sd:approve: 5", "sd:approve:٥",
        "sd:approve_both:1x", "sd:approveboth:1", "sd:approve_both:1\n", "sd:approve_both", "sd:approve_both:",
        "sd:approve_both:-1", "sd:approve_both:٥", "sd:approve_bothx:1", "sd:approve_:1"])
    async def test_malformed_is_answered_and_nothing_runs(self, data):
        query = _query(data)
        run = await _tap(query)
        run.assert_not_awaited()
        assert "Invalid" in _answer_text(query)
        query.edit_message_text.assert_not_called()

    @pytest.mark.parametrize("data, want", [
        ("sd:approve:5", ("approve", 5)), ("sd:approve_both:5", ("approve_both", 5)),
        ("sd:skip:0012", ("skip", 12)), ("sd:skip:5x", None), ("sd:approve_both:1x", None),
        ("sd:approveboth:1", None), ("sd:approve_both:1\n", None), ("", None)])
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
        ("sd:approve:5", "approved", "✅ Approved"), ("sd:approve_both:5", "approved", "✅ Approved"),
        ("sd:skip:5", "skipped", "❌ Skipped")])
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
    @pytest.mark.parametrize("verb, argv", [
        ("approve", f"approve 5 --card={CARD_ID} --json"),
        ("approve_both", f"approve 5 --placement both --card={CARD_ID} --json"),
        ("skip", "skip 5 --json")])
    async def test_each_verb_runs_its_worker_argv(self, tmp_path, verb, argv):
        settings, workdir = _fake_worker(tmp_path, "echo '{\"ok\": true, \"id\": 5, \"status\": \"approved\"}'")
        answer = await sd.run_worker(verb, 5, settings, card=None if verb == "skip" else CARD_ID)
        assert answer == sd.WorkerAnswer(True, "approved")
        assert (workdir / "argv").read_text().strip() == argv

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


# --- SEC-125: the daily taste batch's one-photo cards ---------------------------------------------------

TASTE_TITLE = "Ceramic table lamp, 25 cm"
KEYBOARD = object()  # stands in for the card's InlineKeyboardMarkup
TASTE_VERBS = [("taste_post", "post", "👍 Would post"), ("taste_never", "never", "👎 Never"),
               ("taste_unsure", "unsure", "🤷 Not sure")]


def _photo_query(data, caption=TASTE_TITLE, user_id=OWNER):
    """A tap on a taste card: a photo message, so it has a caption and no text."""
    query = _query(data, user_id=user_id)
    query.message.text = None
    query.message.text_html = None
    query.message.caption = caption
    query.message.reply_markup = KEYBOARD
    return query


class TestTasteRouting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb, answer, label", TASTE_VERBS)
    async def test_each_taste_verb_runs_the_worker_with_its_ask_id_and_no_card(self, verb, answer, label):
        run = await _tap(_photo_query(f"sd:{verb}:7"), answer=sd.WorkerAnswer(True, answer))
        run.assert_awaited_once_with(verb, 7, SETTINGS, card=None)

    @pytest.mark.parametrize("data, want", [
        ("sd:taste_post:5", ("taste_post", 5)), ("sd:taste_never:5", ("taste_never", 5)),
        ("sd:taste_unsure:0012", ("taste_unsure", 12)), ("sd:taste_post:5x", None), ("sd:taste_maybe:5", None)])
    def test_parse_callback(self, data, want):
        assert sd.parse_callback(data) == want

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", [
        "sd:taste_post", "sd:taste_post:", "sd:taste_post:abc", "sd:taste_post:5:6", "sd:taste_post:-5",
        "sd:taste_post: 5", "sd:taste_post:5\n", "sd:taste_post:٥", "sd:TASTE_POST:5", "sd:taste:5",
        "sd:taste_maybe:5", "sd:taste_postx:5", "sd:taste_:5", "sd:taste_post_never:5", "sd:taste_answer:5"])
    async def test_malformed_taste_data_is_answered_and_nothing_runs(self, data):
        query = _photo_query(data)
        run = await _tap(query)
        run.assert_not_awaited()
        assert "Invalid" in _answer_text(query)
        query.edit_message_caption.assert_not_called()


class TestTasteAuth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb, answer, label", TASTE_VERBS)
    async def test_non_owner_is_refused(self, verb, answer, label):
        query = _photo_query(f"sd:{verb}:7", user_id="222")
        run = await _tap(query)  # the gateway allowlist admits everyone; the owner gate does not
        run.assert_not_awaited()
        assert _answer_text(query) == unauthorized_action_notice("telegram")
        query.edit_message_caption.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_outside_the_gateway_allowlist_is_refused(self):
        query = _photo_query("sd:taste_post:7")
        run = await _tap(query, allowed_users="111")
        run.assert_not_awaited()
        assert _answer_text(query) == unauthorized_action_notice("telegram")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allowed", [None, "", True])
    async def test_unset_owner_refuses_everyone(self, allowed):
        query = _photo_query("sd:taste_post:7")
        run = await _tap(query, settings={**SETTINGS, "allowed_user_id": allowed})
        run.assert_not_awaited()
        assert "allowed_user_id is not set" in _answer_text(query)
        query.edit_message_caption.assert_not_called()


class TestTasteOutcome:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb, answer, label", TASTE_VERBS)
    async def test_success_edits_the_caption_and_keeps_the_buttons(self, verb, answer, label):
        query = _photo_query(f"sd:{verb}:7")
        await _tap(query, answer=sd.WorkerAnswer(True, answer))
        assert _answer_text(query) == label
        query.edit_message_caption.assert_awaited_once_with(
            caption=f"{TASTE_TITLE}\n\n— {label} by Jonathan", reply_markup=KEYBOARD)
        query.edit_message_text.assert_not_called()  # a photo message has no text to edit
        query.edit_message_reply_markup.assert_not_called()  # the buttons stay

    @pytest.mark.asyncio
    async def test_the_caption_is_plain_text_never_html(self):
        query = _photo_query("sd:taste_post:7", caption="Lamp <b>& co</b>")
        await _tap(query, answer=sd.WorkerAnswer(True, "post"))
        kwargs = query.edit_message_caption.call_args.kwargs
        assert "parse_mode" not in kwargs
        assert kwargs["caption"].startswith("Lamp <b>& co</b>\n\n")

    @pytest.mark.asyncio
    async def test_a_second_tap_replaces_the_status_line_instead_of_stacking(self):
        first = f"{TASTE_TITLE}\n\n— 👍 Would post by Jonathan"
        query = _photo_query("sd:taste_never:7", caption=first)
        await _tap(query, answer=sd.WorkerAnswer(True, "never"))
        assert query.edit_message_caption.call_args.kwargs["caption"] == \
            f"{TASTE_TITLE}\n\n— 👎 Never by Jonathan"

    @pytest.mark.asyncio
    async def test_a_title_that_looks_like_a_status_line_is_kept(self):
        """Only a status line this code wrote (one of the three labels) is replaced, never seller text."""
        title = "Set of 2\n\n— made by hand"
        query = _photo_query("sd:taste_post:7", caption=title)
        await _tap(query, answer=sd.WorkerAnswer(True, "post"))
        assert query.edit_message_caption.call_args.kwargs["caption"] == f"{title}\n\n— 👍 Would post by Jonathan"

    @pytest.mark.asyncio
    async def test_worker_refusal_is_answered_and_the_card_is_not_edited(self):
        query = _photo_query("sd:taste_post:7")
        await _tap(query, answer=sd.WorkerAnswer(False, "taste ask 7 was never sent, so it has no card"))
        assert _answer_text(query) == "taste ask 7 was never sent, so it has no card"
        assert query.answer.call_args.kwargs["show_alert"] is True
        query.edit_message_caption.assert_not_called()
        query.edit_message_reply_markup.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_caption_edit_does_not_strip_the_buttons(self, caplog):
        query = _photo_query("sd:taste_post:7")
        query.edit_message_caption.side_effect = RuntimeError("message is not modified")
        await _tap(query, answer=sd.WorkerAnswer(True, "post"))
        assert _answer_text(query) == "👍 Would post"  # the toast still confirmed the saved answer
        query.edit_message_reply_markup.assert_not_called()
        assert "taste caption edit failed" in caplog.text

    @pytest.mark.asyncio
    async def test_the_approve_path_is_unchanged_by_taste_verbs(self):
        """An approve still edits text and strips the buttons, even on a message that has a caption."""
        query = _photo_query("sd:approve:5")
        query.message.text_html = CARD_HTML
        await _tap(query, answer=sd.WorkerAnswer(True, "approved"))
        assert query.edit_message_text.call_args.kwargs["reply_markup"] is None
        query.edit_message_caption.assert_not_called()


class TestRunWorkerTaste:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb, answer, label", TASTE_VERBS)
    async def test_each_taste_verb_runs_taste_answer_with_json(self, tmp_path, verb, answer, label):
        settings, workdir = _fake_worker(tmp_path, f"echo '{{\"ok\": true, \"ask_id\": 7, \"status\": \"{answer}\"}}'")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret-canary"}):
            result = await sd.run_worker(verb, 7, settings)
        assert result == sd.WorkerAnswer(True, answer)
        assert (workdir / "argv").read_text().strip() == f"taste answer 7 {answer} --json"
        assert "secret-canary" not in (workdir / "env").read_text()

    @pytest.mark.asyncio
    async def test_a_taste_tap_never_passes_a_card_or_placement(self, tmp_path):
        settings, workdir = _fake_worker(tmp_path, "echo '{\"ok\": true, \"status\": \"post\"}'")
        await sd.run_worker("taste_post", 7, settings, card=CARD_ID)
        argv = (workdir / "argv").read_text()
        assert "--card" not in argv and "--placement" not in argv

    @pytest.mark.asyncio
    async def test_refusal_returns_the_workers_detail(self, tmp_path):
        payload = {"ok": False, "error": "not_found", "detail": "no taste ask 7"}
        settings, _ = _fake_worker(tmp_path, f"echo '{json.dumps(payload)}'\nexit 2")
        assert await sd.run_worker("taste_post", 7, settings) == sd.WorkerAnswer(False, "no taste ask 7")

    @pytest.mark.asyncio
    async def test_timeout_names_the_taste_card_not_a_candidate(self, tmp_path):
        settings, _ = _fake_worker(tmp_path, "exec sleep 5")
        answer = await sd.run_worker("taste_post", 7, settings, timeout=0.3)
        assert not answer.ok and "unknown" in answer.text and "taste card 7" in answer.text
        assert "candidate" not in answer.text
