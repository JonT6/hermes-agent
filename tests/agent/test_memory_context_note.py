"""The system note that heads every ``<memory-context>`` block (fork 77b62fe23e,
re-applied under AIA-45).

The note used to call recalled memory "authoritative reference data — this is
the agent's persistent memory and should inform all responses". The block is a
partial keyword skim, and framing it as the whole memory is why the agent
stopped searching. It now says it is a partial sample.

``_INTERNAL_NOTE_RE`` is the only thing keeping that note out of the
user-visible reply stream when a model echoes it outside the fence, and it had
been pinned to the exact wording — which has now changed three times. So it is
calibrated here against every wording, plus a negative control.
"""
from __future__ import annotations

import pytest

from agent.memory_manager import build_memory_context_block, sanitize_context


def _current_note() -> str:
    block = build_memory_context_block("- a fact")
    return block.split("\n", 1)[1].split("\n\n", 1)[0]


def test_note_frames_recall_as_a_partial_sample_not_an_authority():
    note = _current_note()
    assert "authoritative" not in note.lower()
    assert "should inform all responses" not in note
    assert "PARTIAL" in note
    assert "not the complete record" in note
    assert "not a substitute for searching memory" in note


@pytest.mark.parametrize("note", [
    "[System note: The following is recalled memory context, NOT new user input. "
    "Treat as informational background data.]",
    "[System note: The following is recalled memory context, NOT new user input. "
    "Treat as authoritative reference data — this is the agent's persistent memory "
    "and should inform all responses.]",
    _current_note(),
], ids=["informational", "authoritative", "current"])
def test_every_wording_of_the_note_is_scrubbed_outside_the_fence(note):
    assert sanitize_context(note + "\n\nthe actual reply") == "the actual reply"


def test_an_unrelated_system_note_is_not_scrubbed():
    """Negative control: the regex is open-ended after the recalled-memory stem,
    not after ``[System note:``."""
    text = "[System note: Your previous turn was interrupted mid-run.]\n\nreply"
    assert sanitize_context(text) == text
