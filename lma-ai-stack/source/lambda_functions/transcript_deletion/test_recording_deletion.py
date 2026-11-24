#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for deleting a meeting's recordings after the summary.

The same function as the transcript's deletion, with its own setting. DynamoDB,
S3 and EventBridge Scheduler are in-memory fakes; the table's fake evaluates the
conditions the recordings' writes use, so a link added between a read and a
write is seen as DynamoDB would see it.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

FUNCTION_DIR = Path(__file__).resolve().parent
LAYER = FUNCTION_DIR.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(FUNCTION_DIR), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

import lambda_function as lf  # noqa: E402
from test_lambda_function import (  # noqa: E402
    BUCKET,
    CALL_ID,
    CONTEXT,
    FakeScheduler,
    _conflict,
)

NAME = lf.posixify_filename(CALL_ID)
HOST = f"https://{BUCKET}.s3.us-east-1.amazonaws.com"
AUDIO_1 = f"lma-audio-recordings/{NAME}.wav"
AUDIO_2 = f"lma-audio-recordings/{NAME}_s2.wav"
# A Virtual Participant names its file from the meeting name and the time, and
# stores its link URL-encoded.
VP_AUDIO = "lma-audio-recordings/Weekly sync 2026-11-02 09.05.wav"
VIDEO_1 = f"lma-video-recordings/{NAME}_2026-11-02T09-30-00-000Z.mp4"
VIDEO_2 = f"lma-video-recordings/{NAME}_s2_2026-11-02T10-30-00-000Z.mp4"


def _url(key: str) -> str:
    return f"{HOST}/{quote(key)}"


class FakeTable:
    """The event sourcing table, evaluating the conditions the recordings' writes use."""

    def __init__(self):
        self.items = {}
        # Called once before the next update_item, to stand for a write that
        # lands between the function's read and its own write.
        self.before_next_update = None

    def put(self, **item):
        self.items[(item["PK"], item["SK"])] = dict(item)

    def get_item(self, Key, **_):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": json.loads(json.dumps(item))} if item else {}

    def query(self, KeyConditionExpression, **_):
        partition = KeyConditionExpression.get_expression()["values"][1]
        return {"Items": [dict(v) for k, v in sorted(self.items.items()) if k[0] == partition]}

    def batch_writer(self):
        table = self

        class _Writer:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def delete_item(self, Key):
                table.items.pop((Key["PK"], Key["SK"]), None)

        return _Writer()

    @staticmethod
    def _holds(item, condition, names, values):
        for clause in condition.split(" AND "):
            clause = clause.strip()
            if clause == "attribute_exists(PK)":
                ok = item is not None
            elif clause.startswith("attribute_not_exists("):
                field = clause[len("attribute_not_exists(") : -1]
                ok = names.get(field, field) not in (item or {})
            elif clause.startswith("size("):
                field, value = clause[len("size(") :].split(") = ")
                ok = len((item or {}).get(names[field], [])) == values[value]
            elif " = " in clause:
                field, value = clause.split(" = ")
                ok = (item or {}).get(names.get(field, field)) == values[value]
            else:
                raise AssertionError(f"unexpected condition {clause!r}")
            if not ok:
                return False
        return True

    def update_item(
        self,
        Key,
        UpdateExpression,
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        if self.before_next_update:
            hook, self.before_next_update = self.before_next_update, None
            hook()
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        item = self.items.get((Key["PK"], Key["SK"]))
        condition = ConditionExpression.replace("#status", "Status")
        if not self._holds(item, condition, {**names, "#status": "Status"}, values):
            raise _conflict("ConditionalCheckFailedException")
        if UpdateExpression in (
            "SET RecordingDeletionScheduledFor = :at",
            "SET TranscriptDeletionScheduledFor = :at",
        ):
            item[UpdateExpression.split()[1]] = values[":at"]
        elif UpdateExpression == "REMOVE RecordingDeletionScheduledFor":
            item.pop("RecordingDeletionScheduledFor", None)
        elif UpdateExpression.startswith("SET RecordingsDeletedAt"):
            item.setdefault("RecordingsDeletedAt", values[":now"])
            item.setdefault("RecordingDeletionTrigger", values[":trigger"])
            item["RecordingDeletionLastRunAt"] = values[":now"]
            item["RecordingFilesDeleted"] = item.get("RecordingFilesDeleted", 0) + values[":files"]
            removed = UpdateExpression.split("REMOVE ", 1)[1].split(", ")
            for field in removed:
                item.pop(names.get(field, field), None)
        else:
            raise AssertionError(f"unexpected update {UpdateExpression!r}")


class FakeS3:
    """A versioned bucket: each key's versions, a version id starting "dm" being a delete marker."""

    def __init__(self):
        self.versions = {}
        self.fail_deletes = False

    def put(self, key, count=1, markers=0):
        listed = self.versions.setdefault(key, [])
        listed.extend(f"v{len(listed) + n}" for n in range(count))
        listed.extend(f"dm{n}" for n in range(markers))

    def get_paginator(self, _name):
        s3 = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                assert Bucket == BUCKET
                entries = [
                    {"Key": key, "VersionId": version}
                    for key, versions in s3.versions.items()
                    if key.startswith(Prefix)
                    for version in versions
                ]
                yield {
                    "Versions": [e for e in entries if not e["VersionId"].startswith("dm")],
                    "DeleteMarkers": [e for e in entries if e["VersionId"].startswith("dm")],
                }

        return _Paginator()

    def delete_objects(self, Bucket, Delete):
        assert Bucket == BUCKET
        if self.fail_deletes:
            return {"Errors": [{"Code": "AccessDenied", "Message": "denied"}]}
        for entry in Delete["Objects"]:
            versions = self.versions.get(entry["Key"], [])
            if entry["VersionId"] in versions:
                versions.remove(entry["VersionId"])
            if not versions:
                self.versions.pop(entry["Key"], None)
        return {}


#: Files that are not recordings, or are another meeting's, and must be kept.
KEPT = [
    f"lma-video-keyframes/{NAME}/0000001000-a.jpg",
    f"lma-video-keyframes/{NAME}/0000001000-a-thumb.jpg",
    f"lma-chat-attachments/{NAME}/att-1/notes.pdf",
    f"lma-consent-evidence/{NAME}/reply-1.jpg",
    f"lma-transcripts/{NAME}-TRANSCRIPT.txt",
    f"lma-transcripts/{NAME}-SUMMARY.txt",
    # Other meetings, whose names begin with this one's.
    f"lma-audio-recordings/{NAME}1.wav",
    f"lma-audio-recordings/{NAME}_minutes.wav",
    f"lma-video-chunks/{CALL_ID}1/chunk-0.webm",
]


@pytest.fixture
def aws(monkeypatch):
    table, s3, scheduler = FakeTable(), FakeS3(), FakeScheduler()
    monkeypatch.setattr(lf, "_clients", {"table": table, "s3": s3, "scheduler": scheduler})
    monkeypatch.setattr(lf, "RECORDINGS_BUCKET", BUCKET)
    monkeypatch.setattr(lf, "SCHEDULE_GROUP_NAME", "stack-TranscriptDeletion")
    monkeypatch.setattr(lf, "SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/scheduler")
    monkeypatch.setattr(lf, "DEAD_LETTER_QUEUE_ARN", "arn:aws:sqs:us-east-1:123456789012:dlq")
    monkeypatch.setattr(lf, "IS_TRANSCRIPT_SUMMARY_ENABLED", True)
    # The transcript's own setting is off throughout, unless a test says otherwise.
    monkeypatch.setattr(lf, "current_delay", lambda: None)
    table.put(
        PK=f"c#{CALL_ID}",
        SK=f"c#{CALL_ID}",
        Status="ENDED",
        CallSummaryText='{"SUMMARY": "kept"}',
        # A meeting continued by a rejoin: two sessions of each kind.
        RecordingUrl=_url(AUDIO_2),
        RecordingUrls=[_url(AUDIO_1), _url(AUDIO_2)],
        RecordingUrlSessions=[1, 2],
        VideoRecordingUrl=_url(VIDEO_2),
        VideoRecordingUrls=[_url(VIDEO_1), _url(VIDEO_2)],
        VideoRecordingUrlSessions=[1, 2],
    )
    for n in range(3):
        table.put(PK=f"trs#{CALL_ID}", SK=f"s#{n}", Transcript=f"said {n}")
    s3.put(AUDIO_1, count=2)
    s3.put(AUDIO_1[: -len(".wav")] + ".raw")
    s3.put(AUDIO_2)
    s3.put(AUDIO_2[: -len(".wav")] + ".raw")
    s3.put(VIDEO_1, count=3)
    s3.put(VIDEO_2)
    for key in KEPT:
        s3.put(key)
    return SimpleNamespace(table=table, s3=s3, scheduler=scheduler)


def _set_delay(monkeypatch, minutes):
    monkeypatch.setattr(lf, "current_recording_delay", lambda: minutes)


def _meeting(aws):
    return aws.table.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")]


def _recording_keys(aws):
    return sorted(
        k
        for k in aws.s3.versions
        if k.startswith(("lma-audio-recordings/", "lma-video-recordings/", "lma-video-chunks/"))
        and k not in KEPT
    )


def _schedule(aws, kind):
    return aws.scheduler.schedules.get(lf.schedule_name(CALL_ID, kind))


def _minutes_from_now(definition) -> float:
    moment = datetime.strptime(definition["ScheduleExpression"], "at(%Y-%m-%dT%H:%M:%S)")
    return (moment.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 60


def _run(trigger, **extra):
    return lf.handler({"CallId": CALL_ID, "Trigger": trigger, **extra}, CONTEXT)


def _scheduled_recordings():
    return _run("scheduled", Kind="recordings")


# ── what is deleted ─────────────────────────────────────────────────────────


def test_deletes_every_version_of_every_sessions_audio_and_video(aws):
    outcome = _scheduled_recordings()
    assert _recording_keys(aws) == []
    # Two WAVs with their raw audio, and two videos.
    assert outcome == {"action": "deleted", "filesDeleted": 6}


def test_keeps_key_frames_attachments_consent_evidence_the_transcript_and_other_meetings(aws):
    _scheduled_recordings()
    assert all(key in aws.s3.versions for key in KEPT)
    assert len([k for k in aws.table.items if k[0] == f"trs#{CALL_ID}"]) == 3
    assert _meeting(aws)["CallSummaryText"] == '{"SUMMARY": "kept"}'


def test_removes_the_links_and_stamps_the_meeting(aws):
    _scheduled_recordings()
    meeting = _meeting(aws)
    for field in lf.AUDIO_LINKS + lf.VIDEO_LINKS + lf.LINK_SESSION_FIELDS:
        assert field not in meeting, field
    assert meeting["RecordingsDeletedAt"].endswith("Z")
    assert meeting["RecordingDeletionTrigger"] == "scheduled"
    assert meeting["RecordingFilesDeleted"] == 6
    assert "TranscriptDeletedAt" not in meeting


def test_delete_markers_are_deleted_too(aws):
    aws.s3.put(AUDIO_1, markers=2)
    _scheduled_recordings()
    assert AUDIO_1 not in aws.s3.versions


def test_a_virtual_participant_recording_is_found_through_its_encoded_link(aws):
    aws.s3.put(VP_AUDIO)
    _meeting(aws)["RecordingUrls"].append(_url(VP_AUDIO))
    _scheduled_recordings()
    assert VP_AUDIO not in aws.s3.versions


def test_an_unlinked_transcriber_recording_of_the_meeting_is_deleted_by_its_name(aws):
    # Stored beside a session's own recording, which keeps the link.
    beside = f"lma-audio-recordings/{NAME}_2026-11-02T09-31-00-000Z"
    beside_2 = f"lma-audio-recordings/{NAME}_s2_2026-11-02T10-31-00-000Z"
    for key in (beside, beside_2):
        aws.s3.put(key + ".wav")
        aws.s3.put(key + ".raw")
    _scheduled_recordings()
    assert _recording_keys(aws) == []


def test_links_outside_the_recordings_prefixes_or_the_bucket_are_not_followed(aws):
    meeting = _meeting(aws)
    meeting["RecordingUrls"] += [
        f"https://another-bucket.s3.us-east-1.amazonaws.com/lma-audio-recordings/{NAME}x.wav",
        _url(f"lma-video-keyframes/{NAME}/0000001000-a.jpg"),
        _url(f"lma-audio-recordings/nested/{NAME}.wav"),
    ]
    aws.s3.put(f"lma-audio-recordings/nested/{NAME}.wav")
    _scheduled_recordings()
    assert all(key in aws.s3.versions for key in KEPT)
    assert f"lma-audio-recordings/nested/{NAME}.wav" in aws.s3.versions


def test_the_virtual_participants_video_segments_are_deleted(aws):
    aws.s3.put(f"lma-video-chunks/{CALL_ID}/chunk-0.webm", count=2)
    aws.s3.put(f"lma-video-chunks/{CALL_ID}/chunk-1.webm")
    _scheduled_recordings()
    assert not [k for k in aws.s3.versions if k.startswith(f"lma-video-chunks/{CALL_ID}/")]
    assert f"lma-video-chunks/{CALL_ID}1/chunk-0.webm" in aws.s3.versions


def test_a_file_that_cannot_be_deleted_fails_the_run_and_keeps_the_links(aws):
    aws.s3.fail_deletes = True
    with pytest.raises(RuntimeError, match="could not delete"):
        _scheduled_recordings()
    meeting = _meeting(aws)
    assert meeting["RecordingUrls"] and "RecordingsDeletedAt" not in meeting


def test_a_link_added_during_the_deletion_has_its_recording_deleted_too(aws):
    late = f"lma-video-recordings/{NAME}_s3_2026-11-02T11-00-00-000Z.mp4"
    aws.s3.put(late)

    def a_session_three_video_is_linked():
        meeting = _meeting(aws)
        meeting["VideoRecordingUrl"] = _url(late)
        meeting["VideoRecordingUrls"].append(_url(late))
        meeting["VideoRecordingUrlSessions"].append(3)

    aws.table.before_next_update = a_session_three_video_is_linked
    outcome = _scheduled_recordings()
    assert late not in aws.s3.versions
    assert "VideoRecordingUrls" not in _meeting(aws)
    assert outcome["filesDeleted"] == 7
    assert _meeting(aws)["RecordingFilesDeleted"] == 7


def test_links_that_never_stop_changing_fail_the_run_so_it_is_retried(aws, monkeypatch):
    def always_another():
        _meeting(aws)["RecordingUrls"].append(_url(AUDIO_1))
        aws.table.before_next_update = always_another

    aws.table.before_next_update = always_another
    with pytest.raises(lf.RecordingLinksKeptChanging):
        _scheduled_recordings()
    assert "RecordingsDeletedAt" not in _meeting(aws)


def test_a_meeting_with_no_record_still_has_its_recordings_deleted(aws):
    del aws.table.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")]
    outcome = _scheduled_recordings()
    # Only the transcriber's files are found without the record's links.
    assert outcome["filesDeleted"] == 4
    assert VIDEO_1 in aws.s3.versions


def test_a_second_run_keeps_the_first_time_and_adds_to_the_count(aws):
    _scheduled_recordings()
    first = _meeting(aws)["RecordingsDeletedAt"]
    _scheduled_recordings()
    assert _meeting(aws)["RecordingsDeletedAt"] == first
    assert _meeting(aws)["RecordingFilesDeleted"] == 6


# ── when ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("trigger", ["meeting-ended", "summary-written"])
def test_nothing_happens_while_the_setting_is_off(aws, monkeypatch, trigger):
    _set_delay(monkeypatch, None)
    assert _run(trigger) == {"action": "none"}
    assert not aws.scheduler.schedules
    assert len(_recording_keys(aws)) == 6


def test_meeting_end_schedules_the_deletion_after_the_summary_wait_and_the_delay(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    outcome = _run("meeting-ended")
    assert outcome["recordings"]["action"] == "scheduled"
    definition = _schedule(aws, "recordings")
    assert abs(_minutes_from_now(definition) - (lf.SUMMARY_WAIT_MINUTES + 15)) < 1.1
    assert json.loads(definition["Target"]["Input"]) == {
        "CallId": CALL_ID,
        "Trigger": "scheduled",
        "Kind": "recordings",
    }
    assert "recordings" in definition["Description"]
    assert _meeting(aws)["RecordingDeletionScheduledFor"].endswith("Z")
    assert len(_recording_keys(aws)) == 6


def test_meeting_end_without_summaries_counts_the_delay_from_the_end(aws, monkeypatch):
    _set_delay(monkeypatch, 30)
    monkeypatch.setattr(lf, "IS_TRANSCRIPT_SUMMARY_ENABLED", False)
    _run("meeting-ended")
    assert abs(_minutes_from_now(_schedule(aws, "recordings")) - 30) < 1.1


def test_the_summary_moves_the_deletion_earlier_and_never_later(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    _run("meeting-ended")
    _run("summary-written")
    assert abs(_minutes_from_now(_schedule(aws, "recordings")) - 15) < 1.1
    assert _run("meeting-ended")["recordings"]["action"] == "unchanged"
    assert abs(_minutes_from_now(_schedule(aws, "recordings")) - 15) < 1.1


def test_immediately_deletes_on_the_summary_and_sweeps_once_more(aws, monkeypatch):
    _set_delay(monkeypatch, 0)
    outcome = _run("summary-written")
    assert outcome["recordings"]["action"] == "deleted"
    assert _recording_keys(aws) == []
    sweep = _schedule(aws, "recordings")
    assert abs(_minutes_from_now(sweep) - lf.SWEEP_MINUTES) < 1.1
    assert json.loads(sweep["Target"]["Input"])["Kind"] == "recordings"
    assert _meeting(aws)["RecordingDeletionTrigger"] == "summary-written"


def test_a_scheduled_deletion_runs_even_if_the_setting_was_turned_off(aws, monkeypatch):
    _set_delay(monkeypatch, None)
    assert _scheduled_recordings()["action"] == "deleted"


def test_the_two_settings_are_independent(aws, monkeypatch):
    monkeypatch.setattr(lf, "current_delay", lambda: 15)
    _set_delay(monkeypatch, 0)
    outcome = _run("summary-written")
    assert outcome["action"] == "scheduled"
    assert outcome["recordings"]["action"] == "deleted"
    assert _recording_keys(aws) == []
    assert len([k for k in aws.table.items if k[0] == f"trs#{CALL_ID}"]) == 3
    transcript = _schedule(aws, "transcript")
    recordings = _schedule(aws, "recordings")
    assert transcript["Name"] != recordings["Name"]
    assert json.loads(transcript["Target"]["Input"]) == {"CallId": CALL_ID, "Trigger": "scheduled"}


def test_a_scheduled_transcript_deletion_keeps_the_recordings(aws, monkeypatch):
    monkeypatch.setattr(lf, "delete_transcript", lambda call_id, trigger: {"action": "deleted"})
    _run("scheduled")
    assert len(_recording_keys(aws)) == 6
    assert "RecordingsDeletedAt" not in _meeting(aws)


def test_a_failure_of_one_kind_still_runs_the_other_and_fails_the_run(aws, monkeypatch):
    def broken():
        raise RuntimeError("configuration unavailable")

    monkeypatch.setattr(lf, "current_delay", broken)
    _set_delay(monkeypatch, 0)
    with pytest.raises(RuntimeError, match="configuration unavailable"):
        _run("summary-written")
    assert _recording_keys(aws) == []


def test_a_summary_written_while_the_meeting_is_live_deletes_nothing(aws, monkeypatch):
    _set_delay(monkeypatch, 0)
    _meeting(aws).update(Status="STARTED", SessionStartedNumber=2)
    assert _run("summary-written")["recordings"] == {"action": "live"}
    assert len(_recording_keys(aws)) == 6


def test_schedule_names_differ_by_kind_and_fit_the_scheduler_limits():
    names = {lf.schedule_name("x" * 500, kind) for kind in ("transcript", "recordings")}
    assert len(names) == 2
    assert all(len(name) <= 64 for name in names)


# ── a rejoined meeting ──────────────────────────────────────────────────────

REOPENED_2 = {"SessionNumber": 2}


def test_a_reopened_meeting_has_its_pending_deletion_cancelled(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    _run("meeting-ended")
    _meeting(aws).update(Status="STARTED", SessionStartedNumber=2)
    assert _run("meeting-reopened", **REOPENED_2)["recordings"] == {"action": "cancelled"}
    assert _schedule(aws, "recordings") is None
    assert "RecordingDeletionScheduledFor" not in _meeting(aws)


def test_a_reopen_with_the_setting_off_keeps_the_pending_deletion(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    _run("meeting-ended")
    _set_delay(monkeypatch, None)
    _meeting(aws).update(Status="STARTED", SessionStartedNumber=2)
    assert _run("meeting-reopened", **REOPENED_2)["recordings"]["action"] == "kept"
    assert _schedule(aws, "recordings") is not None


def test_a_reopen_after_the_recordings_were_deleted_reports_the_deletion(aws):
    _scheduled_recordings()
    _meeting(aws).update(Status="STARTED", SessionStartedNumber=2)
    assert _run("meeting-reopened", **REOPENED_2)["recordings"]["action"] == "deleted"


# ── a recording stored after the deletion ───────────────────────────────────


def _link_a_late_recording(aws, key):
    aws.s3.put(key)
    meeting = _meeting(aws)
    meeting["RecordingUrl"] = _url(key)
    meeting.setdefault("RecordingUrls", []).append(_url(key))
    meeting.setdefault("RecordingUrlSessions", []).append(1)


def test_a_recording_stored_before_any_deletion_is_left_for_it(aws):
    assert _run("recording-added") == {"action": "none"}
    assert len(_recording_keys(aws)) == 6


def test_a_recording_stored_after_the_deletion_is_deleted_when_it_arrives(aws, monkeypatch):
    _set_delay(monkeypatch, 0)
    _run("summary-written")
    first = _meeting(aws)["RecordingsDeletedAt"]
    late = f"lma-audio-recordings/{NAME}_late.wav"
    _link_a_late_recording(aws, late)
    # Whatever the setting says now: the meeting's recordings were deleted.
    _set_delay(monkeypatch, None)
    outcome = _run("recording-added")
    assert outcome["action"] == "deleted"
    assert late not in aws.s3.versions
    meeting = _meeting(aws)
    assert "RecordingUrl" not in meeting and "RecordingUrls" not in meeting
    assert meeting["RecordingsDeletedAt"] == first
    assert meeting["RecordingFilesDeleted"] == 7


def test_a_late_recording_is_deleted_at_once_even_while_a_sweep_is_pending(aws, monkeypatch):
    _set_delay(monkeypatch, 0)
    _run("summary-written")
    assert _meeting(aws)["RecordingDeletionScheduledFor"]
    late = f"lma-audio-recordings/{NAME}_s2_late.wav"
    _link_a_late_recording(aws, late)
    _run("recording-added")
    assert late not in aws.s3.versions


def test_a_later_sessions_recording_of_a_deleted_meeting_is_deleted_when_it_arrives(aws):
    _scheduled_recordings()
    _meeting(aws).update(Status="STARTED", SessionStartedNumber=3)
    later = f"lma-video-recordings/{NAME}_s3_2026-11-02T12-00-00-000Z.mp4"
    aws.s3.put(later)
    _meeting(aws).update(VideoRecordingUrl=_url(later), VideoRecordingUrls=[_url(later)])
    _run("recording-added")
    assert later not in aws.s3.versions
    assert "VideoRecordingUrls" not in _meeting(aws)
