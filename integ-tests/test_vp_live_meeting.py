# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A Virtual Participant in a real meeting that nobody else attends.

``test_meet_end_to_end.py`` puts a scripted guest in the meeting to speak and
chat, which is what it takes to check the transcript. This module needs no
guest. It checks the parts of a Virtual Participant's life that a real meeting
platform decides and that nothing else in the suite reaches: joining,
transcription starting, the meeting record, and the two ways a meeting ends.

Each test launches a Virtual Participant with ``client.vp.create``, the path
``lma vp create`` uses and the Web UI's Create form follows (the
``createVirtualParticipant`` mutation, then the scheduler state machine), and
waits for it to be admitted (``JOINED``) and transcribing (``ACTIVE``) with a
meeting record ``c#<CallId>`` in the EventSourcing table. Then:

``test_vp_leaves_a_meeting_it_is_alone_in``
    Leaves the VP alone. Its attendee watchdog ends the meeting after about a
    minute: the row must reach ``COMPLETED`` with the ``attendees-left``
    trigger, and the meeting record ``ENDED``. A VP that stays in an empty
    meeting runs, and bills, until its duration limit.
``test_vp_ended_from_lma_leaves_the_meeting``
    Ends the VP as the End button does (``endVirtualParticipant``): the row
    must show the test's end reason and a clean terminal status, and the
    meeting record ``ENDED``. Under the ECS launch types (``FARGATE``, ``EC2``)
    the status must be ``COMPLETED``, which the stopped task's shutdown handler
    writes over the mutation's ``ENDED``. Under ``MICROVM`` the MicroVM is
    deleted, and whether the handler runs first is not known, so ``ENDED`` is
    accepted as well.

What it cannot check without another participant: what the VP posts in the
meeting chat (its introduction, and the consent notice where consent is on) is
kept out of its own transcript, and nobody else is there to read it; and there
is no speech to transcribe. The guest test covers both.

Opt-in, with ``LMA_TEST_VP_LIVE_MEETING=1``. The meeting comes from a Secrets
Manager secret, ``lma/integ-tests/live-meeting`` unless
``LMA_TEST_LIVE_MEETING_SECRET_ID`` names another (see ``live_meeting.py`` for
its format, and docs/scheduled-integration-tests.md for the meeting's settings):

    AWS_PROFILE=default LMA_TEST_VP_LIVE_MEETING=1 \\
      .venv/bin/python -m pytest integ-tests/test_vp_live_meeting.py --stack-name lma-integtest1 -s

Once opted in, nothing here skips: a missing secret, a VP that does not join,
and a VP that does not leave are each a failure. The module is marked
``external_meeting`` so that a run without the flag, the scheduled one
included, reports it as skipped rather than failed. About five minutes; each
test runs one Virtual Participant and a few seconds of Amazon Transcribe.

Every Virtual Participant the module starts is ended in a ``finally``, whatever
the test did. The meeting link is never printed: failure messages pass through
``LiveMeeting.redact``.
"""

from __future__ import annotations

from typing import Any

import boto3
import live_meeting
import pytest
from kds_pipeline_probe import _event_sourcing_table
from live_meeting import LiveMeeting, StatusTrail, wait_for
from lma_sdk import LMAClient
from lma_sdk.exceptions import LMAError

pytestmark = [
    pytest.mark.external_meeting,
    pytest.mark.skipif(
        not live_meeting.enabled(),
        reason=f"opt-in: set {live_meeting.ENABLE_VARIABLE}=1 and store the meeting in "
        f"{live_meeting.DEFAULT_SECRET_ID} (or name the secret in {live_meeting.SECRET_VARIABLE})",
    ),
]

USER_NAME = "LMA Integ Test"
# Launches to try before reporting that the VP could not join. Google Meet now
# and then refuses a participant at its Join click, which the VP reports by
# ending without joining; a second launch tells that apart from LMA's join
# failing.
VP_ATTEMPTS = 2
# Placement, browser start and the platform's pre-join screens. The Teams and
# Zoom web clients are the slow ones; a Meet join is usually under two minutes.
LAUNCH_TIMEOUT_S = 300.0
JOIN_TIMEOUT_S = 420.0
# Admitted to transcribing: the intro message, then a Transcribe session.
ACTIVE_TIMEOUT_S = 120.0
MEETING_RECORD_TIMEOUT_S = 90.0
# The watchdog's own bound is three 20-second polls; the rest is slack for a
# participant the platform still shows for a while after it has gone.
ALONE_EXIT_TIMEOUT_S = 300.0
END_TIMEOUT_S = 180.0
POLL_S = 5.0


@pytest.fixture(scope="module")
def meeting(region: str | None) -> LiveMeeting:
    try:
        return live_meeting.resolve(region)
    except live_meeting.LiveMeetingUnavailable as err:
        pytest.fail(str(err), pytrace=False)


@pytest.fixture(scope="module")
def event_table(stack_name: str, region: str | None) -> tuple[Any, str]:
    session = boto3.session.Session(region_name=region) if region else boto3.session.Session()
    table = _event_sourcing_table(
        session.client("cloudformation"), session.client("dynamodb"), stack_name
    )
    return session.client("dynamodb"), table


@pytest.fixture(scope="module")
def vp_launch_type(stack_name: str, region: str | None) -> str | None:
    """The stack's VPLaunchType parameter, or None when it cannot be read."""
    session = boto3.session.Session(region_name=region) if region else boto3.session.Session()
    try:
        stack = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
    except Exception as err:  # noqa: BLE001 - only narrows the expected status
        print(f"could not read VPLaunchType ({type(err).__name__}); accepting ENDED or COMPLETED")
        return None
    return next(
        (p.get("ParameterValue") for p in stack.get("Parameters", []) if p.get("ParameterKey") == "VPLaunchType"),
        None,
    )


class _Participant:
    """One Virtual Participant the test started, ended in ``close`` if still running."""

    def __init__(self, client: LMAClient, meeting: LiveMeeting) -> None:
        self.client = client
        self.meeting = meeting
        self.vp_id: str | None = None
        self.trail = StatusTrail()
        self.attempts: list[str] = []

    def row(self) -> dict[str, Any]:
        row = live_meeting.read_vp(self.client.appsync.graphql, self.vp_id or "")
        if row:
            self.trail.note(row)
        return row

    def fail(self, message: str) -> None:
        pytest.fail(self.meeting.redact(f"{message}\nstatuses seen: {self.trail}"), pytrace=False)

    def join(self, meeting_name: str) -> dict[str, Any]:
        """Launch the VP and wait until it is in the meeting; return its row."""
        for attempt in range(1, VP_ATTEMPTS + 1):
            self.trail = StatusTrail()
            try:
                launch = self.client.vp.create(
                    meeting_name=meeting_name,
                    platform=self.meeting.platform,
                    meeting_id=self.meeting.meeting_id,
                    meeting_password=self.meeting.password,
                    user_name=USER_NAME,
                    wait=True,
                    timeout_s=LAUNCH_TIMEOUT_S,
                )
            except LMAError as err:
                # Not retried: a launch that never places its task is LMA's.
                self.fail(f"the Virtual Participant did not launch: {type(err).__name__}: {err}")
            self.vp_id = launch.id
            row = wait_for(lambda: (r := self.row()) and live_meeting.join_state(r) and r,
                           timeout=JOIN_TIMEOUT_S, interval=POLL_S)
            state = live_meeting.join_state(row) if row else None
            if state == "joined":
                return row
            self.attempts.append(
                f"attempt {attempt}: "
                + (live_meeting.describe(row, self.meeting) if row else "no row")
                + f" (statuses {self.trail})"
            )
            self.close(f"integ-test live meeting: join attempt {attempt} abandoned")
            self.vp_id = None
        self.fail(
            f"the Virtual Participant did not join the meeting in {VP_ATTEMPTS} attempts, "
            f"{int(JOIN_TIMEOUT_S)}s each:\n  " + "\n  ".join(self.attempts)
            + "\nCheck that the meeting admits guests without a host (docs/"
            "scheduled-integration-tests.md), then the VP's logs for the pre-join trigger."
        )
        raise AssertionError("unreachable")  # pytest.fail raises

    def wait_until_transcribing(self) -> dict[str, Any]:
        row = wait_for(lambda: (r := self.row()) and r.get("CallId")
                       and (self.trail.reached(("ACTIVE",)) or r.get("status") in live_meeting.TERMINAL_STATUSES)
                       and r,
                       timeout=ACTIVE_TIMEOUT_S, interval=2.0)
        if not row or not self.trail.reached(("ACTIVE",)):
            self.fail(
                f"the Virtual Participant joined but did not start transcribing (ACTIVE) within "
                f"{int(ACTIVE_TIMEOUT_S)}s; last: {live_meeting.describe(row or {}, self.meeting)}. "
                "If the deployment's Meeting Consent mode is opt-in, capture waits for consent, "
                "which nobody in an empty meeting gives."
            )
        return row

    def close(self, reason: str) -> None:
        if not self.vp_id:
            return
        try:
            if self.row().get("status") not in live_meeting.TERMINAL_STATUSES:
                self.client.vp.end(self.vp_id, reason=reason)
        except Exception as err:  # noqa: BLE001 - cleanup must not mask the test's failure
            print(f"cleanup: could not end Virtual Participant {self.vp_id}: {type(err).__name__}")


def _wait_for_meeting(event_table: tuple[Any, str], call_id: str, status: str | None, timeout: float
                      ) -> dict[str, Any] | None:
    ddb, table = event_table
    return wait_for(
        lambda: (m := live_meeting.read_meeting(ddb, table, call_id))
        and (status is None or m.get("Status") == status) and m,
        timeout=timeout, interval=POLL_S,
    )


def _check_meeting_record(vp: _Participant, event_table: tuple[Any, str], call_id: str) -> None:
    record = _wait_for_meeting(event_table, call_id, None, MEETING_RECORD_TIMEOUT_S)
    if not record:
        vp.fail(
            f"no meeting record c#{call_id} in the EventSourcing table {int(MEETING_RECORD_TIMEOUT_S)}s "
            "after the VP started transcribing: its START event did not reach the Call Event "
            "Processor (Kinesis), or the processor failed to write it"
        )


def _check_meeting_ended(vp: _Participant, event_table: tuple[Any, str], call_id: str) -> None:
    record = _wait_for_meeting(event_table, call_id, "ENDED", END_TIMEOUT_S)
    if not record:
        last = live_meeting.read_meeting(*event_table, call_id) or {}
        vp.fail(
            f"the meeting record c#{call_id} is {last.get('Status')!r}, not 'ENDED', "
            f"{int(END_TIMEOUT_S)}s after the VP left: no END event was processed"
        )


def test_vp_leaves_a_meeting_it_is_alone_in(
    client: LMAClient, meeting: LiveMeeting, event_table: tuple[Any, str]
) -> None:
    vp = _Participant(client, meeting)
    try:
        vp.join("integ-test live meeting (left alone)")
        call_id = vp.wait_until_transcribing()["CallId"]
        _check_meeting_record(vp, event_table, call_id)

        row = wait_for(lambda: (r := vp.row()) and r.get("status") in live_meeting.TERMINAL_STATUSES and r,
                       timeout=ALONE_EXIT_TIMEOUT_S, interval=POLL_S) or vp.row()
        problem = live_meeting.left_on_its_own_problem(row)
        if problem:
            vp.fail(f"{problem}; last: {live_meeting.describe(row, meeting)}")
        _check_meeting_ended(vp, event_table, call_id)
    finally:
        vp.close("integ-test live meeting cleanup")


def test_vp_ended_from_lma_leaves_the_meeting(
    client: LMAClient, meeting: LiveMeeting, event_table: tuple[Any, str], vp_launch_type: str | None
) -> None:
    reason = "integ-test live meeting: ended by the test"
    accepted = live_meeting.statuses_after_end(vp_launch_type)
    vp = _Participant(client, meeting)
    try:
        vp.join("integ-test live meeting (ended from LMA)")
        call_id = vp.wait_until_transcribing()["CallId"]
        # Ended as soon as it is transcribing, well inside the minute after
        # which a VP alone in a meeting leaves by itself. The meeting record is
        # checked afterwards: its START event has already been sent.
        before = vp.row()
        if before.get("status") in live_meeting.TERMINAL_STATUSES:
            vp.fail(
                "the Virtual Participant had already left before the test could end it, so the "
                f"end path was not exercised; last: {live_meeting.describe(before, meeting)}"
            )
        client.vp.end(vp.vp_id, reason=reason)
        _check_meeting_record(vp, event_table, call_id)

        def settled() -> dict[str, Any] | None:
            r = vp.row()
            return r if live_meeting.ended_from_lma_problem(r, reason, accepted) is None else None

        row = wait_for(settled, timeout=END_TIMEOUT_S, interval=POLL_S) or vp.row()
        problem = live_meeting.ended_from_lma_problem(row, reason, accepted)
        if problem:
            vp.fail(
                f"{problem} (VPLaunchType {vp_launch_type or 'unknown'}); "
                f"last: {live_meeting.describe(row, meeting)}"
            )
        _check_meeting_ended(vp, event_table, call_id)
        print(
            f"ended from LMA under VPLaunchType {vp_launch_type or 'unknown'}: status "
            f"{row.get('status')} when checked (statuses seen: {vp.trail})"
        )
    finally:
        vp.close("integ-test live meeting cleanup")
