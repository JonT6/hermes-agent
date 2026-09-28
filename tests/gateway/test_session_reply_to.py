"""SEC-44: the message a turn replies to reaches tools as a gateway-bound ContextVar.

The secret-deals plugin binds an approval to the card it answers, so it needs the replied-to message id
from the gateway, never from the model. ``HERMES_SESSION_REPLY_TO_MESSAGE_ID`` is bound per turn from the
inbound event and cleared with the other session vars."""

from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.session_context import (
    _UNSET,
    _VAR_MAP,
    clear_session_vars,
    reset_session_vars,
    set_session_reply_to,
    set_session_vars,
)
from tests.gateway.test_internal_notification_marker import SESSION_KEY, _bootstrap, _source

VAR = "HERMES_SESSION_REPLY_TO_MESSAGE_ID"


@pytest.fixture(autouse=True)
def _clean():
    reset_session_vars()
    yield
    reset_session_vars()


def test_set_and_clear_session_vars_carry_the_reply_to_id():
    tokens = set_session_vars(platform="telegram", reply_to_message_id="4321")
    assert _VAR_MAP[VAR].get() == "4321"
    clear_session_vars(tokens)
    assert _VAR_MAP[VAR].get() == ""


def test_unbound_until_a_turn_binds_it():
    assert _VAR_MAP[VAR].get() is _UNSET


@pytest.mark.parametrize("value, want", [("4321", "4321"), (4321, "4321"), (None, ""), ("", "")])
def test_set_session_reply_to_normalises(value, want):
    set_session_reply_to(value)
    assert _VAR_MAP[VAR].get() == want


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to, want", [("4321", "4321"), (None, "")])
async def test_an_agent_turn_binds_the_events_reply_to_id(monkeypatch, tmp_path, reply_to, want):
    runner = _bootstrap(monkeypatch, tmp_path)
    seen = []

    async def run_agent(*_a, **_kw):
        seen.append(_VAR_MAP[VAR].get())  # what a tool running in this turn would read
        return {"final_response": "ack", "messages": [], "tools": [], "history_offset": 0, "last_prompt_tokens": 0}

    runner._run_agent = AsyncMock(side_effect=run_agent)
    event = MessageEvent(text="approve", source=_source(), message_id="msg-1", reply_to_message_id=reply_to)
    await runner._handle_message_with_agent(event, _source(), SESSION_KEY, 1)
    assert seen == [want]
