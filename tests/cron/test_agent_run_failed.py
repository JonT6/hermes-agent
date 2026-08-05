"""AIA-13: a cron turn that attempted tools and failed every one is not a success.

Measured 2026-08-05 across seven unattended runs. Two distinct routes, one
signature -- the job logs 'completed successfully' having done nothing:

    20:35  tool_call wrapper fumbled  -> gave up after 1 try, 51 chars
    22:12  a2a_call called directly with missing args -> gave up, 25 chars

The 22:12 run happened AFTER pinning the tool resident, which is what proves
this is not a tool_search problem. It is the absence of any rule saying that a
turn whose every tool attempt errored did not succeed.

The predicate lives here, alone and named, because run_job is 1103 lines and
its success decision was the residue of eleven separate issue patches
(#4219 #17855 #23979 #33465 #34452 #44585 #53027 #58720 #62002 #63142 #69396).
A gap between accumulated exceptions is invisible; a named predicate is not.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from cron.scheduler import _agent_run_failed


def _tool(name, content):
    return {'role': 'tool', 'name': name, 'tool_name': name, 'content': content}


def _ok_result(messages=None, **over):
    r = {'failed': False, 'completed': True, 'turn_exit_reason': '',
         'final_response': 'done', 'messages': messages or []}
    r.update(over)
    return r


class TestExistingFailureSignalsStillFail:
    def test_failed_true_is_a_failure(self):
        assert _agent_run_failed(_ok_result(failed=True, error='boom')) == 'boom'

    def test_not_completed_is_a_failure(self):
        assert _agent_run_failed(_ok_result(completed=False)) is not None

    def test_max_iterations_with_a_summary_is_not_a_failure(self):
        r = _ok_result(completed=False,
                       turn_exit_reason='max_iterations_reached(150)',
                       final_response='here is what I found')
        assert _agent_run_failed(r) is None

    def test_a_normal_turn_is_not_a_failure(self):
        assert _agent_run_failed(_ok_result()) is None


class TestEveryToolCallErrored:
    def test_the_only_tool_call_errored(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            _tool('a2a_call', "Error: both 'agent' and 'message' are required."),
        ], final_response='PINPROBE-TOOL-UNAVAILABLE')
        reason = _agent_run_failed(r)
        assert reason is not None
        assert 'a2a_call' in reason

    def test_the_bridge_wrapper_error_counts_too(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            _tool('tool_call', '{"error": "tool_call to a2a_call is missing required argument(s): agent, message. The tool was NOT invoked."}'),
        ])
        assert _agent_run_failed(r) is not None

    def test_one_success_among_failures_is_not_a_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'ask leo'},
            _tool('a2a_call', 'Error: both required.'),
            _tool('a2a_call', 'Leo says: 0.3.0'),
        ])
        assert _agent_run_failed(r) is None

    def test_a_turn_with_no_tool_calls_is_not_a_failure(self):
        r = _ok_result(messages=[{'role': 'user', 'content': 'just write a poem'}])
        assert _agent_run_failed(r) is None

    def test_only_the_current_turn_is_examined(self):
        """A prior turn's failed tool call must not condemn this one."""
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'old request'},
            _tool('a2a_call', 'Error: both required.'),
            {'role': 'user', 'content': 'new request'},
            _tool('a2a_call', 'Leo says: 0.3.0'),
        ])
        assert _agent_run_failed(r) is None

    def test_missing_messages_key_does_not_raise(self):
        r = {'failed': False, 'completed': True, 'turn_exit_reason': '',
             'final_response': 'x'}
        assert _agent_run_failed(r) is None


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
        assert _agent_run_failed(r) is None

    def test_a_tool_reporting_a_real_error_result_is_not_a_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'fetch it'},
            _tool('web_search', '{"error": "upstream returned 503"}'),
        ], final_response='Upstream is returning 503.')
        assert _agent_run_failed(r) is None

    def test_a_missing_file_is_an_answer_not_a_job_failure(self):
        r = _ok_result(messages=[
            {'role': 'user', 'content': 'read the log'},
            _tool('read_file', 'Error: File not found: /var/log/nope.log'),
        ], final_response='That log does not exist.')
        assert _agent_run_failed(r) is None
