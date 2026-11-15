# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting the live Virtual Participant test joins, and how its run is judged.

``test_vp_live_meeting.py`` sends a Virtual Participant into a real meeting that
nobody else attends. This module holds the parts of it that need no AWS, so
``test_ci_plumbing.py`` can test them in the fast pipeline:

* **The gate.** The test runs only with ``LMA_TEST_VP_LIVE_MEETING=1``. Without
  it the test skips, and the skip is allowed under ``--no-skips`` because the
  module carries the ``external_meeting`` marker. Once opted in, nothing skips:
  a missing, unreadable or malformed secret is a failure.
* **The meeting.** It comes from a Secrets Manager secret,
  ``lma/integ-tests/live-meeting`` unless ``LMA_TEST_LIVE_MEETING_SECRET_ID``
  names another, read with the credentials the suite already uses. A meeting
  link lets anyone who holds it join, so it is kept out of CI variables, out of
  the repository and out of the test's output: ``LiveMeeting`` does not show
  its values in its ``repr``, and ``LiveMeeting.redact`` takes them out of any
  text the test prints, such as a status message the Virtual Participant wrote.
* **The verdicts.** Pure functions from a Virtual Participant row (as
  ``getVirtualParticipant`` returns it) to a problem description, or ``None``.

The secret is a JSON object, or a bare Google Meet link::

    {"url": "https://meet.google.com/abc-defg-hij"}
    {"platform": "ZOOM", "meeting_id": "1234567890", "password": "..."}

``platform`` defaults to ``GOOGLE_MEET``. ``url`` and ``meeting_id`` are
interchangeable; a Google Meet value must be a meeting code or a link to one,
which is the rule the Virtual Participant itself applies.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

ENABLE_VARIABLE = "LMA_TEST_VP_LIVE_MEETING"
SECRET_VARIABLE = "LMA_TEST_LIVE_MEETING_SECRET_ID"
DEFAULT_SECRET_ID = "lma/integ-tests/live-meeting"

# The Virtual Participant's platforms, as lma_sdk names them. Repeated rather
# than imported so this module, and the plumbing tests of it, need no SDK.
PLATFORMS = ("ZOOM", "TEAMS", "CHIME", "WEBEX", "GOOGLE_MEET")
_MEETING_KEYS = ("url", "meeting_url", "meeting_id", "id")
_PASSWORD_KEYS = ("password", "passcode")

# Statuses a Virtual Participant never moves on from (status-manager.ts).
TERMINAL_STATUSES = ("COMPLETED", "ENDED", "FAILED", "CANCELLED")
# In the meeting: JOINED once admitted, ACTIVE once transcription has started.
IN_MEETING_STATUSES = ("JOINED", "ACTIVE")
# The exit the attendee watchdog takes when the VP is the only participant left
# (decideAttendeeAction in the VP's teams.ts, shared by the Meet handler).
ALONE_TRIGGER = "attendees-left"


class LiveMeetingUnavailable(RuntimeError):
    """The test is enabled, but its meeting cannot be read from the secret."""


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    """Whether the live meeting test is switched on."""
    return env.get(ENABLE_VARIABLE, "").strip().lower() not in ("", "0", "false", "no")


def secret_id(env: Mapping[str, str] = os.environ) -> str:
    """The Secrets Manager secret the meeting is read from."""
    return env.get(SECRET_VARIABLE, "").strip() or DEFAULT_SECRET_ID


@dataclass(frozen=True)
class LiveMeeting:
    """A meeting to send the Virtual Participant to.

    The values are left out of ``repr``, so neither an assertion's introspection
    nor a traceback puts the link in a job log.
    """

    platform: str
    meeting_id: str = field(repr=False)
    password: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return f"LiveMeeting(platform={self.platform!r}, meeting=<redacted>)"

    def redact(self, text: str | None) -> str:
        """``text`` with the meeting's link, code and password replaced."""
        result = text or ""
        for secret in sorted(self._secrets(), key=len, reverse=True):
            result = result.replace(secret, "<meeting>")
        return result

    def _secrets(self) -> list[str]:
        values = [self.meeting_id, self.password]
        code = google_meet_code(self.meeting_id) if self.platform == "GOOGLE_MEET" else None
        if code:
            values += [code, code.replace("-", "")]
        return [v for v in values if v and len(v) >= 4]


def google_meet_code(value: str) -> str | None:
    """The Google Meet code in a code or a meet.google.com link, or None.

    The SDK's ``google_meet_code`` when it can be imported, so the test and the
    Virtual Participant read a link the same way.
    """
    try:
        from lma_sdk.operations.virtual_participant import (  # pylint: disable=import-outside-toplevel
            google_meet_code as sdk_google_meet_code,
        )
    except ImportError:  # pragma: no cover - the suite always has the SDK
        return None
    return sdk_google_meet_code(value)


def parse_secret(raw: str, source: str) -> LiveMeeting:
    """The meeting described by a secret's value.

    ``source`` names the secret in error messages, which never quote the value.
    """
    text = (raw or "").strip()
    if not text:
        raise LiveMeetingUnavailable(f"secret {source!r} is empty")
    if text[0] in "{[":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise LiveMeetingUnavailable(
                f"secret {source!r} looks like JSON but does not parse "
                f"(line {err.lineno}, column {err.colno})"
            ) from None
        if not isinstance(payload, dict):
            raise LiveMeetingUnavailable(f"secret {source!r} is not a JSON object")
    else:
        payload = {"url": text}

    platform = str(payload.get("platform") or "GOOGLE_MEET").strip().upper()
    if platform not in PLATFORMS:
        raise LiveMeetingUnavailable(
            f"secret {source!r} names platform {platform!r}; expected one of {list(PLATFORMS)}"
        )
    meeting = next((str(payload[k]).strip() for k in _MEETING_KEYS if payload.get(k)), "")
    if not meeting:
        raise LiveMeetingUnavailable(
            f"secret {source!r} has no meeting — expected one of {list(_MEETING_KEYS)}, "
            f"found keys {sorted(payload)}"
        )
    if platform == "GOOGLE_MEET" and not google_meet_code(meeting):
        raise LiveMeetingUnavailable(
            f"secret {source!r} does not hold a Google Meet meeting code or a link to one. "
            "A meeting-nickname (/lookup/) link cannot be joined by a guest; use the "
            "meet.google.com/abc-defg-hij link of the meeting itself"
        )
    password = next((str(payload[k]) for k in _PASSWORD_KEYS if payload.get(k)), "")
    return LiveMeeting(platform=platform, meeting_id=meeting, password=password)


def resolve(
    region: str | None = None,
    env: Mapping[str, str] = os.environ,
    session_factory: Callable[..., Any] | None = None,
) -> LiveMeeting:
    """Read the meeting from Secrets Manager. Raises, never returns None.

    Called only once the test is enabled, so every way of not having a meeting
    is an error with a message that says which one it is.
    """
    name = secret_id(env)
    if session_factory is None:
        import boto3  # pylint: disable=import-outside-toplevel

        session_factory = boto3.Session
    try:
        client = session_factory(region_name=region).client("secretsmanager")
        raw = client.get_secret_value(SecretId=name).get("SecretString")
    except Exception as err:
        raise LiveMeetingUnavailable(
            f"{ENABLE_VARIABLE} is set but Secrets Manager secret {name!r} could not be "
            f"read in region {region or '(default)'}: {type(err).__name__}: {err}. "
            f"Create it (see docs/scheduled-integration-tests.md), set {SECRET_VARIABLE} "
            "to the secret's name, or unset the variable to skip this test."
        ) from err
    if raw is None:
        raise LiveMeetingUnavailable(f"secret {name!r} has no SecretString (binary secrets are not read)")
    return parse_secret(raw, name)


# ── Verdicts over a Virtual Participant row ─────────────────────────────────


def join_state(row: Mapping[str, Any]) -> str | None:
    """``"joined"``, ``"left"`` (ended without joining), or None while joining."""
    status = row.get("status") or ""
    if status in IN_MEETING_STATUSES:
        return "joined"
    if status in TERMINAL_STATUSES:
        return "left"
    return None


def describe(row: Mapping[str, Any], meeting: LiveMeeting | None = None) -> str:
    """One line about a row for a failure message, with the meeting redacted."""
    parts = [f"status={row.get('status')}"]
    for key in ("endTrigger", "endReason", "errorMessage"):
        if row.get(key):
            value = str(row[key])
            parts.append(f"{key}={meeting.redact(value) if meeting else value!r}")
    return ", ".join(parts)


def left_on_its_own_problem(row: Mapping[str, Any]) -> str | None:
    """Why a VP that was alone in the meeting did not leave cleanly, or None.

    Alone, the VP's attendee watchdog ends its meeting after about a minute and
    the container records COMPLETED with the ``attendees-left`` trigger. That
    path runs in the container on every launch type, unlike the end mutation.
    """
    status = row.get("status")
    if status not in TERMINAL_STATUSES:
        return (
            f"the Virtual Participant is still {status} in a meeting with nobody else in it; "
            "it should have left after about a minute (attendee watchdog). If a person or a "
            "leftover participant is in the meeting, the VP correctly stays"
        )
    if status != "COMPLETED":
        return f"the Virtual Participant ended {status}, expected COMPLETED"
    if row.get("endTrigger") != ALONE_TRIGGER:
        return (
            f"the Virtual Participant completed through {row.get('endTrigger')!r}, "
            f"expected {ALONE_TRIGGER!r} (left because it was alone)"
        )
    return None


def statuses_after_end(launch_type: str | None) -> tuple[str, ...]:
    """The final statuses a VP ended through endVirtualParticipant may show.

    The end mutation writes ENDED with the caller's reason, then stops the VP's
    compute. Under FARGATE and EC2 that is an ECS StopTask, whose SIGTERM runs
    the container's shutdown handler, and the handler writes COMPLETED over the
    ENDED: so COMPLETED is required. Under MICROVM the MicroVM is deleted, and
    nothing in the code says whether the handler gets to run first, so either is
    accepted; the same for a launch type this cannot read.
    """
    if (launch_type or "").upper() in ("FARGATE", "EC2"):
        return ("COMPLETED",)
    return ("ENDED", "COMPLETED")


def ended_from_lma_problem(
    row: Mapping[str, Any], reason: str, accepted: Sequence[str] = ("ENDED", "COMPLETED")
) -> str | None:
    """Why a VP ended through endVirtualParticipant does not show it, or None.

    ``accepted`` comes from ``statuses_after_end``. FAILED, or a status that is
    not terminal, is never a clean end.
    """
    status = row.get("status")
    if status not in accepted:
        return (
            f"after endVirtualParticipant the Virtual Participant is {status}, "
            f"expected {' or '.join(accepted)}"
        )
    if row.get("endReason") != reason:
        return f"the row's endReason is {row.get('endReason')!r}, expected the test's {reason!r}"
    return None


# ── Waiting ─────────────────────────────────────────────────────────────────


@dataclass
class StatusTrail:
    """The distinct statuses a polled row went through, for failure messages."""

    seen: list[str] = field(default_factory=list)

    def note(self, row: Mapping[str, Any]) -> None:
        status = row.get("status") or "?"
        if not self.seen or self.seen[-1] != status:
            self.seen.append(status)

    def reached(self, statuses: Sequence[str]) -> bool:
        return any(s in statuses for s in self.seen)

    def __str__(self) -> str:
        return " -> ".join(self.seen) or "(none)"


def wait_for(
    predicate: Callable[[], Any],
    timeout: float,
    interval: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Call ``predicate`` until it returns something truthy or ``timeout`` passes.

    Returns the last value, so a caller can report what it last saw.
    """
    deadline = clock() + timeout
    while True:
        value = predicate()
        if value or clock() >= deadline:
            return value
        sleep(interval)


# ── Reads from the stack ────────────────────────────────────────────────────

VP_QUERY = """
query GetVirtualParticipant($id: ID!) {
  getVirtualParticipant(id: $id) {
    id
    status
    CallId
    errorMessage
    endTrigger
    endReason
  }
}
""".strip()


def read_vp(graphql: Callable[..., dict[str, Any]], vp_id: str) -> dict[str, Any]:
    """The Virtual Participant row, with the exit fields the SDK model omits."""
    data = graphql(query=VP_QUERY, variables={"id": vp_id}) or {}
    return data.get("getVirtualParticipant") or {}


def read_meeting(ddb: Any, table: str, call_id: str) -> dict[str, Any] | None:
    """The meeting record (``c#<CallId>``) in the EventSourcing table, or None."""
    from boto3.dynamodb.types import (
        TypeDeserializer,  # pylint: disable=import-outside-toplevel
    )

    key = {"S": f"c#{call_id}"}
    item = ddb.get_item(TableName=table, Key={"PK": key, "SK": key}).get("Item")
    if not item:
        return None
    deserializer = TypeDeserializer()
    return {k: deserializer.deserialize(v) for k, v in item.items()}
