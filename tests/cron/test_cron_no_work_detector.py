"""AIA-13: a cron turn that attempted tools and had every call rejected did no work.

Measured 2026-08-05 across seven unattended runs. Two distinct routes, one
signature -- the job logs 'completed successfully' having done nothing:

    20:35  tool_call wrapper fumbled  -> gave up after 1 try, 51 chars
    22:12  a2a_call called directly with missing args -> gave up, 25 chars

The 22:12 run happened AFTER pinning the tool resident, which is what proves
this is not a tool_search problem. It is the absence of any rule saying that a
turn whose every tool attempt was rejected did not do its work.

The detector is OBSERVE-ONLY: run_job logs "did no work" at ERROR and lets the
job stand. Upstream's success decision now lives in _final_response_from_result
(the fork's _agent_run_failed predicate was dropped as redundant with it); the
tests below that exercise it pin the contract that the detector never feeds it.

Re-applied onto upstream under AIA-45 (was tests/cron/test_agent_run_failed.py).
"""
import logging
import os

import pytest

import cron.scheduler as cron_scheduler
from cron.scheduler import (
    _call_was_never_invoked,
    _every_tool_call_errored,
    _final_response_from_result,
)


# The direct-route rejection, verbatim. The fork's fixture here was 'Error: both required.',
# which matches no marker -- so the two tests using it passed with the logic they guard removed.
_REJECTED = "Error: both 'agent' and 'message' are required."


def _tool(name, content):
    return {'role': 'tool', 'name': name, 'tool_name': name, 'content': content}


def _ok_result(messages=None, **over):
    r = {'failed': False, 'completed': True, 'turn_exit_reason': '',
         'final_response': 'done', 'messages': messages or []}
    r.update(over)
    return r


def _final(result):
    return _final_response_from_result(result, 'job-id', 'job-name', None)


class TestExistingFailureSignalsStillFail:
    def test_failed_true_is_a_failure(self):
        with pytest.raises(RuntimeError, match='^boom$'):
            _final(_ok_result(failed=True, error='boom'))

    def test_not_completed_is_a_failure(self):
        with pytest.raises(RuntimeError):
            _final(_ok_result(completed=False))

    def test_max_iterations_with_a_summary_is_not_a_failure(self):
        r = _ok_result(completed=False,
                       turn_exit_reason='max_iterations_reached(150)',
                       final_response='here is what I found')
        assert _final(r) == 'here is what I found'

    def test_a_normal_turn_is_not_a_failure(self):
        assert _final(_ok_result()) == 'done'


class TestCallWasNeverInvoked:
    """Pinned to the live emitters of both measured routes, not to copies of their text."""

    def test_the_bridge_route_string_emitted_today_matches(self, monkeypatch):
        import json
        from tools.registry import registry
        from tools.tool_search_validation import validate_deferred_call_args

        schema = {'type': 'function', 'function': {
            'name': 'a2a_call',
            'parameters': {'type': 'object', 'required': ['agent', 'message'],
                           'properties': {'agent': {'type': 'string'},
                                          'message': {'type': 'string'}}}}}
        real = registry.get_schema
        monkeypatch.setattr(registry, 'get_schema',
                            lambda n: schema if n == 'a2a_call' else real(n))
        err = validate_deferred_call_args('a2a_call', {})
        assert err is not None, 'validator stopped rejecting a call with missing args'
        assert _call_was_never_invoked(json.loads(err)['error'])

    def test_the_direct_route_string_emitted_today_matches(self):
        from plugins.platforms.a2a.tools import a2a_call
        assert _call_was_never_invoked(a2a_call({}))

    def test_a_tool_that_ran_is_not_matched(self):
        assert not _call_was_never_invoked('{"exit_code": 1, "error": "connection refused"}')


class TestEveryToolCallErrored:
    def test_the_only_tool_call_errored(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            _tool('a2a_call', "Error: both 'agent' and 'message' are required."),
        ], final_response='PINPROBE-TOOL-UNAVAILABLE')
        reason = _every_tool_call_errored(r)
        assert reason is not None
        assert 'a2a_call' in reason

    def test_the_bridge_wrapper_error_counts_too(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            _tool('tool_call', '{"error": "tool_call to a2a_call is missing required argument(s): agent, message. The tool was NOT invoked."}'),
        ])
        assert _every_tool_call_errored(r) is not None

    def test_one_success_among_failures_is_not_a_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            _tool('a2a_call', _REJECTED),
            _tool('a2a_call', 'Leo says: 0.3.0'),
        ])
        assert _every_tool_call_errored(r) is None

    def test_a_turn_with_no_tool_calls_is_not_a_failure(self):
        r = _ok_result(messages=[{'role': 'user', 'content': 'just write a poem'}])
        assert _every_tool_call_errored(r) is None

    def test_only_the_current_turn_is_examined(self):
        """A prior turn's failed tool call must not condemn this one."""
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'old request'},
            _tool('a2a_call', _REJECTED),
            {'role': 'user', 'content': 'new request'},
            _tool('a2a_call', 'Leo says: 0.3.0'),
        ])
        assert _every_tool_call_errored(r) is None

    def test_a_prior_turns_rejection_does_not_reach_a_tool_free_turn(self):
        """The previous test's current turn succeeds, which vetoes on its own; this one
        has no tool call at all, so only the walk-back keeps the old rejection out."""
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'old request'},
            _tool('a2a_call', _REJECTED),
            {'role': 'user', 'content': 'new request'},
            {'role': 'assistant', 'content': 'a poem'},
        ])
        assert _every_tool_call_errored(r) is None

    def test_unjudgeable_non_string_content_is_not_a_failure(self):
        """Multimodal / untrusted-wrapped results cannot be judged, so they veto."""
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            {'role': 'tool', 'name': 'vision', 'content': [{'type': 'text', 'text': 'ok'}]},
            _tool('a2a_call', _REJECTED),
        ])
        assert _every_tool_call_errored(r) is None

    def test_missing_messages_key_does_not_raise(self):
        r = {'failed': False, 'completed': True, 'turn_exit_reason': '',
             'final_response': 'x'}
        assert _every_tool_call_errored(r) is None


class TestAToolThatRanIsNotAJobFailure:
    '''The false positive that would have been worse than the bug.

    A watchdog that pings a service, gets an error, and reports 'service down'
    is a job working correctly -- the tool error IS the answer. Failing those
    would turn every legitimately-failing health check into a nightly false
    alarm. Only calls rejected BEFORE they ran are agent mistakes.
    '''

    def test_a_terminal_command_that_exited_nonzero_is_not_a_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'check the service'},
            _tool('terminal', '{"exit_code": 1, "error": "connection refused"}'),
        ], final_response='The service is down: connection refused.')
        assert _every_tool_call_errored(r) is None

    def test_a_tool_reporting_a_real_error_result_is_not_a_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'fetch it'},
            _tool('web_search', '{"error": "upstream returned 503"}'),
        ], final_response='Upstream is returning 503.')
        assert _every_tool_call_errored(r) is None

    def test_a_missing_file_is_an_answer_not_a_job_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'read the log'},
            _tool('read_file', 'Error: File not found: /var/log/nope.log'),
        ], final_response='That log does not exist.')
        assert _every_tool_call_errored(r) is None


def _never_invoked():
    return _ok_result(messages=[
        {'role': 'user', 'content': 'ask leo'},
        _tool('a2a_call', "Error: both 'agent' and 'message' are required."),
    ], final_response='PINPROBE-TOOL-UNAVAILABLE')


class _NoWorkAgent:
    def __init__(self, *args, **kwargs):
        pass

    def run_conversation(self, prompt, **_kwargs):
        return _never_invoked()

    def close(self):
        pass


class _SessionDB:
    def __init__(self, *args, **kwargs):
        pass

    def set_session_title(self, *args, **kwargs):
        return True

    def get_compression_tip(self, session_id):
        return None

    def session_lifecycle_statuses(self, session_ids):
        return {sid: 'complete' for sid in session_ids}

    def end_session(self, session_id, reason):
        pass

    def close(self):
        pass


def _run_no_work_job(monkeypatch, tmp_path):
    """run_job end to end with an agent whose only tool call was rejected."""
    import hermes_state
    import run_agent

    monkeypatch.setattr(hermes_state, 'SessionDB', _SessionDB)
    monkeypatch.setattr(run_agent, 'AIAgent', _NoWorkAgent)
    monkeypatch.setattr('hermes_constants.resolve_reasoning_config', lambda *_a, **_k: None)
    # The runtime key is read from the environment (never a literal here);
    # AIAgent and SessionDB are fakes above, so the value is never used.
    monkeypatch.setenv('HERMES_TEST_RUNTIME_KEY', 'unused-placeholder')
    monkeypatch.setattr(
        'hermes_cli.runtime_provider.resolve_runtime_provider',
        lambda **_k: {'api_key': os.environ.get('HERMES_TEST_RUNTIME_KEY', ''),
                      'base_url': None, 'provider': 'test-provider',
                      'api_mode': None, 'command': None, 'args': None})
    monkeypatch.setattr('tools.mcp_tool_discovery.discover_mcp_tools', lambda: [])
    monkeypatch.setattr(cron_scheduler, '_get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(cron_scheduler, 'get_fallback_chain', lambda _cfg: [])
    monkeypatch.setattr(cron_scheduler, '_guard_job_credential_exfil', lambda _job: None)
    return cron_scheduler.run_job({'id': 'no-work', 'name': 'No work',
                                   'prompt': 'ask leo', 'schedule_display': 'manual'})


class TestObserveOnly:
    '''The log-only contract, asserted rather than assumed.

    The detector keys off substrings of arbitrary tool output, chosen from two
    observed errors out of a registry of ~100 tools. Nobody has evidence about
    what they match across the rest of that surface, so a detection must NOT
    fail a job yet. Promoting it later should require deleting
    test_but_the_job_is_not_failed -- which is the point: it makes the promotion
    a deliberate act.
    '''

    def test_the_detector_fires(self):
        assert _every_tool_call_errored(_never_invoked()) is not None

    def test_but_the_job_is_not_failed(self):
        assert _final(_never_invoked()) == 'PINPROBE-TOOL-UNAVAILABLE'

    def test_run_job_logs_did_no_work_and_still_succeeds(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level(logging.ERROR, logger=cron_scheduler.logger.name):
            success, _output, final_response, error = _run_no_work_job(monkeypatch, tmp_path)
        assert success is True and error is None
        assert final_response == 'PINPROBE-TOOL-UNAVAILABLE'
        no_work = [r for r in caplog.records if 'did no work' in r.getMessage()]
        assert len(no_work) == 1
        assert no_work[0].levelno == logging.ERROR
        assert 'a2a_call' in no_work[0].getMessage()
        assert "'No work' (ID: no-work)" in no_work[0].getMessage()
