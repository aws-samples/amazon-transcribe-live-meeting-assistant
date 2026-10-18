# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The speaker-naming pass must be invoked from exactly one place per meeting.

There are two callers, and they are mutually exclusive by construction: the
summary orchestrator runs the pass synchronously just before summarising (which
is what gets the names into the summary), and the Kinesis event processor queues
it only when summaries are switched off. Both firing would pay for two model
calls per meeting and have two writes race each other.

That exclusivity was a blocking review finding once already, and it is enforced
by a condition and a comment rather than by anything executable: importing
`call_event_processor` needs gql and Powertools, so the behaviour cannot be
driven in a unit test. These are source-level invariants instead, in the same
spirit as the CloudFormation template invariants beside them -- they cannot prove
the behaviour, but they do fail if somebody removes the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[1] / "source" / "lambda_functions"
EVENT_PROCESSOR = (
    LAMBDAS / "call_event_processor" / "event_processor" / "call_event_processor.py"
)
ORCHESTRATOR = LAMBDAS / "async_transcript_summary_orchestrator" / "lambda_function.py"


@pytest.fixture(scope="module")
def event_processor() -> str:
    return EVENT_PROCESSOR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def orchestrator() -> str:
    return ORCHESTRATOR.read_text(encoding="utf-8")


def _invoke_block(source: str) -> str:
    """The speaker-name resolver invocation, with its guard and arguments.

    Anchored on the call itself -- `FunctionName=SPEAKER_NAME_RESOLVER_ARN` --
    not on the first mention of that name, which is the module-level environment
    read in both files.
    """
    call = source.index("FunctionName=SPEAKER_NAME_RESOLVER_ARN")
    # Back up to the nearest enclosing guard, and forward past the payload.
    start = source.rindex("\n    if ", 0, call)
    # Past the whole payload: it spans several lines in one of the two callers.
    end = source.index("\n        )", source.index("Payload", call))
    return source[start:end]


def test_the_event_processor_only_queues_the_pass_when_summaries_are_off(
    event_processor: str,
) -> None:
    """Otherwise both callers fire and the meeting pays for two model calls."""
    block = _invoke_block(event_processor)
    assert "not IS_TRANSCRIPT_SUMMARY_ENABLED" in block


def test_the_event_processor_never_waits_for_the_pass(event_processor: str) -> None:
    """A synchronous invoke here blocks the event loop, and with it every other
    record in the same Kinesis batch -- including live segments for other
    meetings. It was also retried by the client's own policy, turning one slow
    pass into four."""
    block = _invoke_block(event_processor)
    assert 'InvocationType="Event"' in block
    assert "RequestResponse" not in block


def test_the_event_processor_does_not_requeue_a_meeting_that_already_ended(
    event_processor: str,
) -> None:
    block = _invoke_block(event_processor)
    assert "not was_already_ended" in block


def test_the_orchestrator_runs_the_pass_before_it_summarises(
    orchestrator: str,
) -> None:
    """The ordering IS the feature: the summary reads the transcript through
    FetchTranscript, which resolves stored names, so naming has to have
    happened first.

    Compared inside the handler, not across the file -- both functions are
    defined above it, in the other order.
    """
    handler = orchestrator[orchestrator.index("def handler(") :]
    assert handler.index("name_speakers(") < handler.index("get_call_summary(")


def test_the_orchestrator_waits_for_the_pass(orchestrator: str) -> None:
    assert 'InvocationType="RequestResponse"' in _invoke_block(orchestrator)


def test_the_orchestrator_uses_a_client_that_cannot_retry_the_pass(
    orchestrator: str,
) -> None:
    """The shared client's socket timeout is shorter than the resolver's own
    timeout and it retries three times, so one slow pass became four complete
    passes -- four model calls, four writes racing each other."""
    config = re.search(
        r"SPEAKER_NAME_CLIENT_CONFIG = BotoCoreConfig\((.*?)\)\n", orchestrator, re.S
    )
    assert config, "the dedicated client config is gone"
    assert '"max_attempts": 1' in config.group(1)
    assert "read_timeout" in config.group(1)


def test_both_callers_identify_the_pass_as_the_final_one(
    event_processor: str, orchestrator: str
) -> None:
    """`Trigger: meeting-end` is what lets the pass skip the cooldown, the
    growth gate and the cost shortcut -- all three of which exist to pace a
    browser timer and would otherwise decline the one pass the summary needs."""
    for source in (event_processor, orchestrator):
        assert '"Trigger": "meeting-end"' in _invoke_block(source)


def test_the_resolver_timeout_is_shorter_than_the_callers_socket_timeout() -> None:
    """So the caller can never abandon a pass that is still running and start
    another."""
    template = (
        Path(__file__).resolve().parents[1] / "deployment" / "lma-ai-stack.yaml"
    ).read_text(encoding="utf-8")
    function = template[template.index("  SpeakerNameResolverFunction:") :]
    timeout = int(re.search(r"\n      Timeout: (\d+)", function).group(1))
    socket_timeout = int(
        re.search(r"SPEAKER_NAME_READ_TIMEOUT: (\d+)", template).group(1)
    )
    assert timeout < socket_timeout, (
        f"the resolver may run for {timeout}s but its caller gives up at "
        f"{socket_timeout}s, so a slow pass would be retried"
    )
