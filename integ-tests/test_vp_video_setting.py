# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting Recording › Record Virtual Participant meeting video, end to end.

See docs/virtual-participant.md#meeting-video-recording for the behaviour
asserted. No meeting is needed: the Virtual Participants here are scheduled a
day ahead and cancelled, or launched against a meeting ID that does not exist
and ended once they have started. The Meeting Recording section of the active
configuration profile is changed for the length of the module and put back in
its teardown (meeting_consent_probe.ConsentSettingsOverride), so a run that
fails part-way leaves the deployment's setting as it found it.

``test_the_form_reads_the_setting``
    getVirtualParticipantVideoRecording, which the Create Virtual Participant
    form reads to disable its Record meeting video switch, answers false.
``test_create_records_why_the_video_will_not_be_recorded``
    A Virtual Participant scheduled a day ahead that asks for video is stored
    with videoRecordingOffReason "configuration" by createVirtualParticipant,
    and with its own enableVideoRecording kept as asked.
``test_the_vp_applies_the_setting_saved_when_it_starts``
    A Virtual Participant created while the setting is on (so its row has no
    reason), then launched after the setting is turned off, against a meeting
    ID that does not exist: the reason on its row can only have been written by
    the Virtual Participant itself when it started. Takes a few minutes, for
    the launch.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from lma_sdk import LMAClient
from lma_sdk.exceptions import LMAError

OWNER = "integ-test@lma.aws"
STALE_MEETING_ID = "0000000000"
#: How long the launched Virtual Participant has to start and write its record.
VP_START_TIMEOUT_S = 300.0

VP_FIELDS = "id status enableVideoRecording videoRecordingOffReason"
CREATE_VP = f"""
mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {{
  createVirtualParticipant(input: $input) {{ {VP_FIELDS} }}
}}
"""
GET_VP = f"""
query GetVirtualParticipant($id: ID!) {{
  getVirtualParticipant(id: $id) {{ {VP_FIELDS} }}
}}
"""
GET_SETTING = "query { getVirtualParticipantVideoRecording }"


@pytest.fixture(scope="module")
def recording(client: LMAClient):
    """The Meeting Recording section, saved now and put back at the end."""
    from meeting_consent_probe import ConsentSettingsOverride, Stack

    override = ConsentSettingsOverride(Stack.find(client.stack_name, client.region), section="recording")
    override.save()
    try:
        yield override
    finally:
        override.restore()


def _end(client: LMAClient, vp_id: str | None, reason: str) -> None:
    """End a VP the test made; a reason naming "cancelled" also deletes a schedule."""
    if not vp_id:
        return
    for attempt in range(3):
        try:
            client.vp.end(vp_id, reason=reason)
            return
        except LMAError as err:
            print(f"ending Virtual Participant {vp_id} failed (attempt {attempt + 1}): {err}")
            time.sleep(5)
    pytest.fail(f"could not end Virtual Participant {vp_id}; end it on the Virtual Participants page")


def _create(client: LMAClient, name: str, **extra: Any) -> dict:
    return client.appsync.graphql(
        CREATE_VP,
        {
            "input": {
                "meetingName": name,
                "meetingPlatform": "ZOOM",
                "meetingId": STALE_MEETING_ID,
                "owner": OWNER,
                "enableVideoRecording": True,
                **extra,
            }
        },
    )["createVirtualParticipant"]


def _row(client: LMAClient, vp_id: str) -> dict:
    return client.appsync.graphql(GET_VP, {"id": vp_id})["getVirtualParticipant"]


def test_the_form_reads_the_setting(client: LMAClient, recording) -> None:
    recording.apply(recordVpMeetingVideo=False)
    assert client.appsync.graphql(GET_SETTING)["getVirtualParticipantVideoRecording"] is False
    recording.apply(recordVpMeetingVideo=True)
    assert client.appsync.graphql(GET_SETTING)["getVirtualParticipantVideoRecording"] is True


def test_create_records_why_the_video_will_not_be_recorded(client: LMAClient, recording) -> None:
    recording.apply(recordVpMeetingVideo=False)
    vp_id = None
    try:
        created = _create(
            client,
            "integ-test (VP video setting, never launched)",
            status="SCHEDULED",
            isScheduled=True,
            meetingTime=int(time.time()) + 24 * 3600,
        )
        vp_id = created["id"]
        row = _row(client, vp_id)
        assert row["videoRecordingOffReason"] == "configuration", row
        assert row["enableVideoRecording"] is True, "the request is kept as it was made"

        # A request that turns the video off itself is not given a reason.
        declined = _create(
            client,
            "integ-test (VP video setting, declined, never launched)",
            status="SCHEDULED",
            isScheduled=True,
            meetingTime=int(time.time()) + 24 * 3600,
            enableVideoRecording=False,
        )
        try:
            assert _row(client, declined["id"])["videoRecordingOffReason"] is None
        finally:
            _end(client, declined["id"], "integ-test VP video setting: cancelled before launch")
    finally:
        _end(client, vp_id, "integ-test VP video setting: cancelled before launch")


def test_the_vp_applies_the_setting_saved_when_it_starts(client: LMAClient, recording) -> None:
    recording.apply(recordVpMeetingVideo=True)
    vp_id = None
    try:
        name = "integ-test (VP video setting, stale meeting)"
        vp_id = _create(client, name, status="INITIALIZING")["id"]
        assert _row(client, vp_id)["videoRecordingOffReason"] is None, "created while the setting is on"

        recording.apply(recordVpMeetingVideo=False)
        client.vp._start_scheduler(  # the SDK's launch step, after the create above
            vp_id=vp_id,
            meeting_name=name,
            platform="ZOOM",
            meeting_id=STALE_MEETING_ID,
            user_name="LMA Integ Test",
        )
        deadline = time.monotonic() + VP_START_TIMEOUT_S
        row = _row(client, vp_id)
        while row.get("videoRecordingOffReason") is None and time.monotonic() < deadline:
            time.sleep(5)
            row = _row(client, vp_id)
        assert row.get("videoRecordingOffReason") == "configuration", (
            f"the Virtual Participant did not record why its video is off within {VP_START_TIMEOUT_S:.0f} s "
            f"of its launch (status {row.get('status')}): it did not start, or the deployed image "
            "predates the setting"
        )
    finally:
        _end(client, vp_id, "integ-test VP video setting cleanup")
