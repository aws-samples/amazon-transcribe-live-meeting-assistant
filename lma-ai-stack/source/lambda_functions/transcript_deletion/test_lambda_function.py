#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for the transcript deletion function.

DynamoDB, S3, EventBridge Scheduler and the knowledge base are in-memory fakes,
so the handler runs end to end without AWS.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

FUNCTION_DIR = Path(__file__).resolve().parent
LAYER = FUNCTION_DIR.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(FUNCTION_DIR), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

import lambda_function as lf  # noqa: E402

CALL_ID = "Weekly sync - 2026-11-02-09:00:00.000"
BUCKET = "recordings"
FUNCTION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:stack-TranscriptDeletion"
CONTEXT = SimpleNamespace(invoked_function_arn=FUNCTION_ARN)


def _conflict(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class FakeTable:
    """The event sourcing table: items by (PK, SK), queried two items a page."""

    PAGE = 2

    def __init__(self):
        self.items = {}

    def put(self, **item):
        self.items[(item["PK"], item["SK"])] = dict(item)

    def query(self, KeyConditionExpression, ExclusiveStartKey=None, **_):
        partition = KeyConditionExpression.get_expression()["values"][1]
        keys = sorted(k for k in self.items if k[0] == partition)
        if ExclusiveStartKey:
            keys = [k for k in keys if k > (ExclusiveStartKey["PK"], ExclusiveStartKey["SK"])]
        page = keys[: self.PAGE]
        response = {"Items": [dict(self.items[k]) for k in page]}
        if len(keys) > self.PAGE:
            response["LastEvaluatedKey"] = {"PK": page[-1][0], "SK": page[-1][1]}
        return response

    def get_item(self, Key, **_):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **_):
        item = self.items.get((Key["PK"], Key["SK"]))
        if item is None:
            raise _conflict("ConditionalCheckFailedException")
        values = ExpressionAttributeValues
        if UpdateExpression.startswith("SET TranscriptDeletionScheduledFor"):
            item["TranscriptDeletionScheduledFor"] = values[":at"]
            return
        item.setdefault("TranscriptDeletedAt", values[":now"])
        item.setdefault("TranscriptDeletionTrigger", values[":trigger"])
        item["TranscriptDeletionLastRunAt"] = values[":now"]
        item["TranscriptSegmentsDeleted"] = (
            item.get("TranscriptSegmentsDeleted", 0) + values[":segments"]
        )
        item["TranscriptFilesDeleted"] = item.get("TranscriptFilesDeleted", 0) + values[":files"]
        item.pop("IssuesDetected", None)
        item.pop("TranscriptDeletionScheduledFor", None)

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


class FakeS3:
    """A versioned bucket: each key holds a list of version ids."""

    def __init__(self):
        self.versions = {}
        self.fail_deletes = False

    def put(self, key, count=1):
        self.versions.setdefault(key, []).extend(f"v{n}" for n in range(count))

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
                yield {"Versions": entries}

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


class FakeScheduler:
    def __init__(self):
        self.schedules = {}

    def create_schedule(self, **definition):
        if definition["Name"] in self.schedules:
            raise _conflict("ConflictException")
        self.schedules[definition["Name"]] = definition

    def update_schedule(self, **definition):
        self.schedules[definition["Name"]] = definition


class FakeKnowledgeBase:
    def __init__(self):
        self.deleted = []

    def list_data_sources(self, **_):
        return {"dataSourceSummaries": [{"dataSourceId": "DS1"}]}

    def delete_knowledge_base_documents(self, knowledgeBaseId, dataSourceId, documentIdentifiers):
        self.deleted.append((knowledgeBaseId, dataSourceId, documentIdentifiers))


@pytest.fixture
def aws(monkeypatch):
    table, s3, scheduler, kb = FakeTable(), FakeS3(), FakeScheduler(), FakeKnowledgeBase()
    monkeypatch.setattr(
        lf, "_clients", {"table": table, "s3": s3, "scheduler": scheduler, "bedrock-agent": kb}
    )
    monkeypatch.setattr(lf, "RECORDINGS_BUCKET", BUCKET)
    monkeypatch.setattr(lf, "TRANSCRIPT_KB_ID", "KB12345678")
    monkeypatch.setattr(lf, "SCHEDULE_GROUP_NAME", "stack-TranscriptDeletion")
    monkeypatch.setattr(lf, "SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/scheduler")
    monkeypatch.setattr(lf, "DEAD_LETTER_QUEUE_ARN", "arn:aws:sqs:us-east-1:123456789012:dlq")
    monkeypatch.setattr(lf, "IS_TRANSCRIPT_SUMMARY_ENABLED", True)
    table.put(PK=f"c#{CALL_ID}", SK=f"c#{CALL_ID}", IssuesDetected="quoted words", Agenda="1. Plan")
    for n in range(5):
        table.put(PK=f"trs#{CALL_ID}", SK=f"s#{n}", Channel="CALLER", Transcript=f"said {n}")
    table.put(PK=f"trs#{CALL_ID}", SK="s#answer", Channel="AGENT_ASSISTANT", Transcript="answer")
    table.put(PK=f"trs#{CALL_ID}", SK="s#category", Channel="CATEGORY_MATCH", Transcript="Pricing")
    table.put(PK="trs#another-meeting", SK="s#0", Channel="CALLER", Transcript="kept")
    name = lf.posixify_filename(CALL_ID)
    s3.put(f"lma-transcripts/{name}-TRANSCRIPT.txt", count=3)
    s3.put(f"lma-transcripts/{name}-TRANSCRIPT.txt.metadata.json")
    s3.put(f"lma-transcripts/{name}-SUMMARY.txt")
    s3.put(f"lma-transcripts/{CALL_ID}.transcribe.json")
    return SimpleNamespace(table=table, s3=s3, scheduler=scheduler, kb=kb)


def _set_delay(monkeypatch, minutes):
    monkeypatch.setattr(lf, "current_delay", lambda: minutes)


def _meeting(aws):
    return aws.table.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")]


def _scheduled_at(aws) -> datetime:
    (definition,) = aws.scheduler.schedules.values()
    expression = definition["ScheduleExpression"]
    return datetime.strptime(expression, "at(%Y-%m-%dT%H:%M:%S)").replace(tzinfo=timezone.utc)


def _minutes_from_now(moment: datetime) -> float:
    return (moment - datetime.now(timezone.utc)).total_seconds() / 60


# ── what is deleted ─────────────────────────────────────────────────────────


def test_deletes_every_transcript_item_across_pages_but_category_markers(aws):
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    remaining = sorted(sk for pk, sk in aws.table.items if pk == f"trs#{CALL_ID}")
    assert remaining == ["s#category"]
    assert outcome["segmentsDeleted"] == 6


def test_leaves_other_meetings_transcripts_alone(aws):
    lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    assert ("trs#another-meeting", "s#0") in aws.table.items


def test_deletes_every_version_of_each_transcript_file_and_keeps_the_summary(aws):
    lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    name = lf.posixify_filename(CALL_ID)
    assert sorted(aws.s3.versions) == [f"lma-transcripts/{name}-SUMMARY.txt"]


def test_removes_the_transcript_files_from_the_knowledge_base(aws):
    lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    ((kb_id, data_source, documents),) = aws.kb.deleted
    uris = sorted(document["s3"]["uri"] for document in documents)
    name = lf.posixify_filename(CALL_ID)
    assert (kb_id, data_source) == ("KB12345678", "DS1")
    assert uris == [
        f"s3://{BUCKET}/lma-transcripts/{CALL_ID}.transcribe.json",
        f"s3://{BUCKET}/lma-transcripts/{name}-TRANSCRIPT.txt",
    ]


def test_a_knowledge_base_failure_does_not_fail_the_deletion(aws):
    def refuse(**_):
        raise _conflict("ValidationException")

    aws.kb.delete_knowledge_base_documents = refuse
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    assert outcome["action"] == "deleted"
    assert "TranscriptDeletedAt" in _meeting(aws)


def test_stamps_the_meeting_and_keeps_its_other_fields(aws):
    lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    meeting = _meeting(aws)
    assert meeting["TranscriptDeletionTrigger"] == "scheduled"
    assert meeting["TranscriptSegmentsDeleted"] == 6
    assert meeting["TranscriptFilesDeleted"] == 3
    assert "IssuesDetected" not in meeting
    assert meeting["Agenda"] == "1. Plan"


def test_a_second_run_keeps_the_first_deletion_time(aws):
    lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    first = _meeting(aws)["TranscriptDeletedAt"]
    aws.table.put(PK=f"trs#{CALL_ID}", SK="s#late", Channel="CALLER", Transcript="late")
    lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    assert _meeting(aws)["TranscriptDeletedAt"] == first
    assert _meeting(aws)["TranscriptSegmentsDeleted"] == 7


def test_a_file_that_cannot_be_deleted_fails_the_invocation_for_a_retry(aws):
    aws.s3.fail_deletes = True
    with pytest.raises(RuntimeError, match="could not delete"):
        lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    assert "TranscriptDeletedAt" not in _meeting(aws)


def test_a_meeting_with_no_record_still_has_its_transcript_deleted(aws):
    del aws.table.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")]
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    assert outcome["segmentsDeleted"] == 6


# ── when ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("trigger", ["meeting-ended", "summary-written"])
def test_nothing_happens_while_the_setting_is_off(aws, monkeypatch, trigger):
    _set_delay(monkeypatch, None)
    assert lf.handler({"CallId": CALL_ID, "Trigger": trigger}, CONTEXT) == {"action": "none"}
    assert not aws.scheduler.schedules
    assert len([k for k in aws.table.items if k[0] == f"trs#{CALL_ID}"]) == 7


def test_meeting_end_schedules_the_deletion_after_the_summary_wait_and_the_delay(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    lf.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, CONTEXT)
    assert abs(_minutes_from_now(_scheduled_at(aws)) - (lf.SUMMARY_WAIT_MINUTES + 15)) < 1.1
    assert len([k for k in aws.table.items if k[0] == f"trs#{CALL_ID}"]) == 7


def test_meeting_end_without_summaries_counts_the_delay_from_the_end(aws, monkeypatch):
    _set_delay(monkeypatch, 30)
    monkeypatch.setattr(lf, "IS_TRANSCRIPT_SUMMARY_ENABLED", False)
    lf.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, CONTEXT)
    assert abs(_minutes_from_now(_scheduled_at(aws)) - 30) < 1.1


def test_meeting_end_without_summaries_deletes_immediately_when_asked(aws, monkeypatch):
    _set_delay(monkeypatch, 0)
    monkeypatch.setattr(lf, "IS_TRANSCRIPT_SUMMARY_ENABLED", False)
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, CONTEXT)
    assert outcome["action"] == "deleted"


def test_the_summary_moves_the_deletion_earlier(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    lf.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, CONTEXT)
    lf.handler({"CallId": CALL_ID, "Trigger": "summary-written"}, CONTEXT)
    assert abs(_minutes_from_now(_scheduled_at(aws)) - 15) < 1.1
    recorded = datetime.fromisoformat(
        _meeting(aws)["TranscriptDeletionScheduledFor"].replace("Z", "+00:00")
    )
    assert abs(_minutes_from_now(recorded) - 15) < 1.1


def test_a_scheduled_deletion_is_never_moved_later(aws, monkeypatch):
    _set_delay(monkeypatch, 15)
    lf.handler({"CallId": CALL_ID, "Trigger": "summary-written"}, CONTEXT)
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, CONTEXT)
    assert outcome["action"] == "unchanged"
    assert abs(_minutes_from_now(_scheduled_at(aws)) - 15) < 1.1


def test_immediately_deletes_on_the_summary_and_sweeps_once_more(aws, monkeypatch):
    _set_delay(monkeypatch, 0)
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "summary-written"}, CONTEXT)
    assert outcome["action"] == "deleted"
    assert abs(_minutes_from_now(_scheduled_at(aws)) - lf.SWEEP_MINUTES) < 1.1


def test_a_scheduled_deletion_runs_even_if_the_setting_was_turned_off(aws, monkeypatch):
    _set_delay(monkeypatch, None)
    outcome = lf.handler({"CallId": CALL_ID, "Trigger": "scheduled"}, CONTEXT)
    assert outcome["action"] == "deleted"


def test_the_schedule_invokes_this_function_once_and_then_deletes_itself(aws, monkeypatch):
    _set_delay(monkeypatch, 30)
    qualified = SimpleNamespace(invoked_function_arn=FUNCTION_ARN + ":live")
    lf.handler({"CallId": CALL_ID, "Trigger": "summary-written"}, qualified)
    (definition,) = aws.scheduler.schedules.values()
    assert definition["ActionAfterCompletion"] == "DELETE"
    assert definition["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert definition["Target"]["Arn"] == FUNCTION_ARN
    assert json.loads(definition["Target"]["Input"]) == {"CallId": CALL_ID, "Trigger": "scheduled"}
    assert definition["Target"]["DeadLetterConfig"]["Arn"].endswith(":dlq")


def test_schedule_names_fit_the_scheduler_limits():
    name = lf.schedule_name("x" * 500)
    assert len(name) <= 64
    assert all(c.isalnum() or c in "-_." for c in name)
    assert lf.schedule_name("a") != lf.schedule_name("b")


def test_transcript_file_names_match_the_summary_step():
    assert lf.transcript_file_keys("A b:c-")[0] == "lma-transcripts/A_b_c-TRANSCRIPT.txt"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"CallId": CALL_ID},
        {"CallId": "", "Trigger": "scheduled"},
        {"CallId": CALL_ID, "Trigger": "x"},
    ],
)
def test_rejects_an_event_without_a_meeting_and_a_known_trigger(event):
    with pytest.raises(ValueError):
        lf.handler(event, CONTEXT)


def test_times_are_written_and_read_in_utc():
    assert lf._iso(datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)) == "2026-11-02T09:00:00.000Z"
    assert lf._parse_iso("2026-11-02T09:00:00.000Z") == datetime(
        2026, 11, 2, 9, 0, tzinfo=timezone.utc
    )
    assert lf._parse_iso("not a time") is None


def test_the_setting_is_read_past_the_cache(monkeypatch):
    """A save on the Configuration page applies to the next meeting to end."""
    calls = []

    class Store:
        def invalidate(self):
            calls.append("invalidate")

    monkeypatch.setattr(lf, "_STORE", Store())
    monkeypatch.setattr(lf, "deletion_delay_minutes", lambda store=None: calls.append("read") or 15)
    assert lf.current_delay() == 15
    assert calls == ["invalidate", "read"]
