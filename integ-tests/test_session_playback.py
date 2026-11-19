# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A continued meeting's recording links and session offsets, on a live stack.

See integ-tests/session_playback_probe.py for the mechanics. Self-contained and
self-cleaning: a synthetic meeting, no Virtual Participant, no uploaded file.
"""

from __future__ import annotations

from lma_sdk import LMAClient
from session_playback_probe import SESSION_2_OFFSET, SessionPlaybackProbe, wait_for


def test_each_sessions_recording_is_stored_with_its_session_and_offset(client: LMAClient) -> None:
    probe = SessionPlaybackProbe.for_stack(client.stack_name, region=client.region)
    try:
        probe.send_start()
        assert wait_for(probe.meeting, bool, 90), f"no meeting record for {probe.call_id}"

        # Session 1's link, from an event that names its session.
        probe.send_recording(1, name_the_session=True)
        meeting = wait_for(probe.meeting, lambda m: m.get("RecordingUrlSessions"), 90)
        assert meeting.get("RecordingUrls") == [probe.link(1)]
        assert meeting.get("RecordingUrlSessions") == [1], (
            "the recording link was stored without its session: the call event "
            "processor or updateCall.request.vtl predates per-session links"
        )

        # Session 2, allowed as a rejoin allows it, then started by its START.
        probe.allow_session(2, SESSION_2_OFFSET)
        probe.send_start(2)
        meeting = wait_for(probe.meeting, lambda m: m.get("SessionStartedNumber") == 2, 90)
        assert meeting.get("SessionStartedNumber") == 2, "session 2's START did not reopen the meeting"
        sessions = meeting.get("Sessions") or []
        assert [(s.get("SessionNumber"), s.get("OffsetSeconds")) for s in sessions] == [
            (2, SESSION_2_OFFSET)
        ], f"session 2 is not listed with its offset: {sessions}"

        # Session 2's link, from an event that does not name its session (as a
        # streaming transcriber sends it): the meeting's current one.
        probe.send_recording(2, name_the_session=False)
        meeting = wait_for(
            probe.meeting, lambda m: len(m.get("RecordingUrlSessions") or []) >= 2, 90
        )
        assert meeting.get("RecordingUrls") == [probe.link(1), probe.link(2)]
        assert meeting.get("RecordingUrlSessions") == [1, 2]
        assert meeting.get("RecordingUrl") == probe.link(2)
        assert meeting.get("SessionNumber") == 2, "a recording link changed the allowed session"
    finally:
        probe.cleanup()
