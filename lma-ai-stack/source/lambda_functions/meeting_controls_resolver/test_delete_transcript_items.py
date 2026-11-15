# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting deletes every item of its transcript.

The items are read from the table by partition, not through
getTranscriptSegments, so final and partial segments go alike, and so do
segments with a channel that query does not return, such as the category
markers LMA before 0.4.0 stored with the channel CATEGORY_MATCH.

Same setup as test_delete_metering_records.py: no AWS, the AppSync client and
the GraphQL DSL replaced before import, a fake table that pages its results.
"""

from __future__ import annotations

import pytest
from test_delete_metering_records import FakeTable, index

ROWS = [
    {"PK": "trs#m-1", "SK": "ts#1#final", "Channel": "CALLER", "IsPartial": False},
    {"PK": "trs#m-1", "SK": "ts#2#partial", "Channel": "AGENT", "IsPartial": True},
    {"PK": "trs#m-1", "SK": "ts#3#category", "Channel": "CATEGORY_MATCH", "IsPartial": False},
    {"PK": "trs#m-1", "SK": "ts#4#assistant", "Channel": "AGENT_ASSISTANT", "IsPartial": False},
    {"PK": "trs#m-1", "SK": "ts#5#no-channel"},
]


def _keys(rows):
    return sorted(({"PK": r["PK"], "SK": r["SK"]} for r in rows), key=lambda k: k["SK"])


def test_every_item_of_the_transcript_is_deleted(monkeypatch) -> None:
    table = FakeTable(ROWS)
    monkeypatch.setattr(index, "ddbTable", table)
    assert index.delete_transcript_items("m-1") == len(ROWS)
    assert sorted(table.deleted, key=lambda k: k["SK"]) == _keys(ROWS)


def test_a_category_marker_and_a_partial_segment_are_deleted(monkeypatch) -> None:
    table = FakeTable(ROWS)
    monkeypatch.setattr(index, "ddbTable", table)
    index.delete_transcript_items("m-1")
    assert {"PK": "trs#m-1", "SK": "ts#3#category"} in table.deleted
    assert {"PK": "trs#m-1", "SK": "ts#2#partial"} in table.deleted


def test_the_query_reads_every_page_of_the_transcript_partition_only(monkeypatch) -> None:
    table = FakeTable(ROWS, page=2)
    monkeypatch.setattr(index, "ddbTable", table)
    index.delete_transcript_items("m-1")
    assert len(table.queries) == 3
    for query in table.queries:
        assert query["ExpressionAttributeValues"] == {":pk": "trs#m-1"}
        # No filter: a filter is what would leave items behind.
        assert "FilterExpression" not in query
        assert query["ConsistentRead"] is True


def test_a_meeting_without_a_transcript_deletes_nothing(monkeypatch) -> None:
    table = FakeTable([])
    monkeypatch.setattr(index, "ddbTable", table)
    assert index.delete_transcript_items("m-1") == 0
    assert table.deleted == []


def test_a_failed_delete_fails_the_meeting_deletion(monkeypatch) -> None:
    class FailingTable(FakeTable):
        def delete_item(self, Key):  # noqa: N803
            if Key["SK"] == "ts#3#category":
                raise RuntimeError("throttled")
            super().delete_item(Key)

    table = FailingTable(ROWS)
    monkeypatch.setattr(index, "ddbTable", table)
    with pytest.raises(RuntimeError, match="throttled"):
        index.delete_transcript_items("m-1")
    # The rest are still attempted.
    assert len(table.deleted) == len(ROWS) - 1


def test_deleting_a_meeting_empties_its_transcript_partition(monkeypatch) -> None:
    """delete_meeting deletes the partition directly, not via getTranscriptSegments."""

    def no_appsync_read(*_args):
        raise AssertionError("getTranscriptSegments is not part of deleting a meeting")

    table = FakeTable(ROWS)
    monkeypatch.setattr(index, "ddbTable", table)
    monkeypatch.setattr(index, "get_transcript_segments", no_appsync_read)
    monkeypatch.setattr(index, "cleanup_virtual_participants", lambda callid: None)
    monkeypatch.setattr(index, "get_call_details", lambda *a: {"getCall": {"SharedWith": None}})
    monkeypatch.setattr(index, "delete_recordings_transcripts", lambda callid: None)
    monkeypatch.setattr(index, "delete_metering_records", lambda callid: 0)
    monkeypatch.setattr(index, "delete_keyframes", lambda callid: 0)
    monkeypatch.setattr(index, "delete_consent_evidence", lambda callid: 0)
    monkeypatch.setattr(index, "delete_video_recordings", lambda callid, url: 0)
    monkeypatch.setattr(index, "dsl_gql", lambda *a: None)
    monkeypatch.setattr(index, "DSLMutation", lambda *a: None)

    class _Field:
        def __getattr__(self, _name):
            return self

        def args(self, **_kwargs):
            return self

        def select(self, *_args):
            return self

    class _Session:
        def execute(self, _query):
            return {}

    index.delete_meeting(_Session(), _Field(), "m-1", "cls#x", "x", "owner@example.com")
    assert sorted(table.deleted, key=lambda k: k["SK"]) == _keys(ROWS)
