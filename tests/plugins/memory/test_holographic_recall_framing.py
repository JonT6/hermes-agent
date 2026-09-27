"""Regression tests for the 2026-08-26 recall-framing fix (fork 77b62fe23e,
re-applied under AIA-45).

Cawl answered from five bullets while ~800 facts sat unread. Retrieval was
fine; the framing was not. Every turn ``prefetch()`` emitted up to five bare
bullets — no ids, no counts, nothing saying other matches existed — and
``build_memory_context_block()`` wrapped them as "authoritative reference data
... the agent's persistent memory". A model handed five facts labelled
authoritative does not spend a tool call to look for a sixth. On a keyword miss
``prefetch()`` returned "", and nothing reads as "memory is empty" rather than
"the search missed".

Also covered, both found while verifying that fix:

* ``retrieval_count`` was dead on the live path — the fact_store tool routes
  through ``FactRetriever``, which never wrote it. It is now recorded when an
  explicit fact_store read returns facts, and deliberately NOT by the automatic
  skim, so it measures deliberate recall.

NOT re-applied: the fork's ``_strip_nul`` (NUL -> space on add_fact). Its own
docstring recorded that no failure was reproduced, and on upstream 59004a6235
the behavioural check below already passes — a NUL-bearing fact stays
searchable on both sides of the byte. The test is kept as a guard: the one
failure the fork DID reproduce is that deleting the byte fuses the neighbouring
tokens, so any future normalisation must not do that.
"""

import json

from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic.store import MemoryStore


def _make_provider(tmp_path, **config):
    base = {"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 64}
    base.update(config)
    provider = HolographicMemoryProvider(config=base)
    provider.initialize(session_id="test-session")
    return provider


def _retrieval_counts(provider):
    return {
        r["fact_id"]: r["retrieval_count"]
        for r in provider._store._conn.execute("SELECT fact_id, retrieval_count FROM facts")
    }


# ---------------------------------------------------------------------------
# prefetch(): an index, not an answer
# ---------------------------------------------------------------------------


def test_prefetch_says_it_is_a_partial_skim_with_counts_and_ids(tmp_path):
    provider = _make_provider(tmp_path)
    fid = provider._store.add_fact("Cawl deploys through supervisorctl on the Mac Mini", tags="deploy")
    provider._store.add_fact("An unrelated fact about coffee brewing ratios")

    block = provider.prefetch("how does cawl deploy")

    assert "PARTIAL skim" in block
    assert "1 shown, drawn from 2 stored facts" in block
    assert f"#{fid}" in block
    assert "Drill handles: deploy" in block
    assert "call fact_store before answering" in block
    provider.shutdown()


def test_prefetch_states_how_many_matches_it_withheld(tmp_path):
    provider = _make_provider(tmp_path)
    for i in range(8):
        provider._store.add_fact(f"deploy note number {i} about the gateway restart")

    block = provider.prefetch("deploy gateway restart")

    assert "5 shown, drawn from 8 stored facts" in block
    assert "At least 3 further match(es) exist that are NOT shown" in block
    assert sum(1 for line in block.splitlines() if line.startswith("- [#")) == 5
    provider.shutdown()


def test_prefetch_on_a_keyword_miss_says_the_search_missed(tmp_path):
    """A miss must not inject nothing: silence reads as 'memory is empty'."""
    provider = _make_provider(tmp_path)
    provider._store.add_fact("Cawl deploys through supervisorctl on the Mac Mini")

    block = provider.prefetch("zebra xylophone quartz")

    assert block != ""
    assert "skim found NOTHING" in block
    assert "KEYWORD SEARCH missed" in block
    assert "not evidence the fact is absent" in block
    provider.shutdown()


def test_prefetch_on_an_empty_store_injects_nothing(tmp_path):
    provider = _make_provider(tmp_path)
    assert provider.prefetch("anything at all") == ""
    provider.shutdown()


def test_counts_exclude_the_untrusted_quarantine(tmp_path):
    """Advertising the AIA-16 quarantine would turn an on-demand escape hatch
    into an automatic one: counts cover the recallable set only."""
    provider = _make_provider(tmp_path)
    provider._store.add_fact("Cawl deploys through supervisorctl on the Mac Mini")
    provider._store.add_fact("peer text about deploys", "untrusted_peer", "", trust_score=0.0)

    assert "drawn from 1 stored facts" in provider.prefetch("deploys")
    assert "Active. 1 facts stored" in provider.system_prompt_block()
    provider.shutdown()


def test_prefetch_does_not_count_as_a_retrieval(tmp_path):
    provider = _make_provider(tmp_path)
    fid = provider._store.add_fact("Cawl deploys through supervisorctl on the Mac Mini")

    assert f"#{fid}" in provider.prefetch("cawl deploys")
    assert _retrieval_counts(provider)[fid] == 0
    provider.shutdown()


# ---------------------------------------------------------------------------
# system_prompt_block(): a trigger list, not a capability note
# ---------------------------------------------------------------------------


def test_system_prompt_block_frames_the_skim_and_forbids_asserting_absence(tmp_path):
    provider = _make_provider(tmp_path)
    provider._store.add_fact("Cawl deploys through supervisorctl on the Mac Mini")

    block = provider.system_prompt_block()

    assert "KEYWORD SKIM" in block
    assert "it is not the memory itself" in block
    assert "When to search memory BEFORE answering" in block
    assert "never assert absence on the strength of the skim" in block
    assert "fact_feedback" in block
    provider.shutdown()


# ---------------------------------------------------------------------------
# retrieval_count: recorded on deliberate fact_store reads
# ---------------------------------------------------------------------------


def test_fact_store_reads_record_retrieval(tmp_path):
    provider = _make_provider(tmp_path)
    fid = provider._store.add_fact("Alice maintains the Hermes gateway deploy scripts")
    other = provider._store.add_fact("An unrelated fact about coffee brewing ratios")

    out = json.loads(provider.handle_tool_call("fact_store", {"action": "search", "query": "gateway deploy"}))
    assert [r["fact_id"] for r in out["results"]] == [fid]
    assert out["count"] == 1
    assert _retrieval_counts(provider) == {fid: 1, other: 0}

    for args in ({"action": "probe", "entity": "Alice"},
                 {"action": "related", "entity": "Alice"},
                 {"action": "reason", "entities": ["Alice"]}):
        out = json.loads(provider.handle_tool_call("fact_store", args))
        assert fid in [r["fact_id"] for r in out["results"]], args
    counts = _retrieval_counts(provider)
    assert counts[fid] == 4
    provider.shutdown()


def test_list_is_browsing_not_recall(tmp_path):
    provider = _make_provider(tmp_path)
    fid = provider._store.add_fact("Cawl deploys through supervisorctl on the Mac Mini")

    out = json.loads(provider.handle_tool_call("fact_store", {"action": "list"}))
    assert out["count"] == 1
    assert _retrieval_counts(provider)[fid] == 0
    provider.shutdown()


def test_record_retrieval_ignores_non_integer_ids(tmp_path):
    store = MemoryStore(db_path=tmp_path / "store.db", hrr_dim=64)
    try:
        fid = store.add_fact("a fact to count")
        store.record_retrieval([fid, fid + 999, "7", None])
        store.record_retrieval([])
        assert store.list_facts()[0]["retrieval_count"] == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# NUL bytes (guard only; see module docstring)
# ---------------------------------------------------------------------------


def test_a_fact_containing_a_nul_byte_stays_searchable_on_both_sides(tmp_path):
    provider = _make_provider(tmp_path)
    fid = provider._store.add_fact("before\x00after")

    for term in ("before", "after"):
        out = json.loads(provider.handle_tool_call("fact_store", {"action": "search", "query": term}))
        assert [r["fact_id"] for r in out["results"]] == [fid], term
    provider.shutdown()
