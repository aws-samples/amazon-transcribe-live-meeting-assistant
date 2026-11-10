# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting deletes every Virtual Participant record that names it.

Same setup as test_delete_metering_records.py: no AWS, the AppSync client and
the GraphQL DSL replaced before import. The Virtual Participant table is a fake
whose scan pages its results the way DynamoDB does: the filter is applied to
each page after it is read, so a page can hold none of the matches.
"""

from __future__ import annotations

import pytest
from test_delete_metering_records import index


class FakeVPTable:
    def __init__(self, rows, page=2, fail_delete=()):
        self.rows = list(rows)
        self.page = page
        self.fail_delete = set(fail_delete)
        self.deleted = []
        self.scans = 0

    def scan(self, FilterExpression, ExclusiveStartKey=None):  # noqa: N803
        self.scans += 1
        start = (ExclusiveStartKey or {}).get("i", 0)
        read = self.rows[start : start + self.page]
        callid = FilterExpression.get_expression()["values"][1]
        out = {"Items": [row for row in read if row.get("CallId") == callid]}
        if start + self.page < len(self.rows):
            out["LastEvaluatedKey"] = {"i": start + self.page}
        return out

    def delete_item(self, Key):  # noqa: N803
        if Key["id"] in self.fail_delete:
            raise RuntimeError(f"cannot delete {Key['id']}")
        self.deleted.append(Key["id"])


class FakeDdb:
    def __init__(self, table):
        self.table = table

    def Table(self, name):  # noqa: N802
        assert name == "vp-table"
        return self.table


@pytest.fixture
def ended(monkeypatch):
    """Patch the table and record what each Virtual Participant is sent."""
    sent = {"end": [], "stop": [], "registry": []}
    monkeypatch.setattr(index, "VP_TABLE_NAME", "vp-table")
    monkeypatch.setattr(index, "send_vp_end_event", lambda c, vp: sent["end"].append(vp["id"]))
    monkeypatch.setattr(index, "stop_vp_ecs_task", lambda vp_id: sent["stop"].append(vp_id))
    monkeypatch.setattr(
        index, "delete_vp_registry_entry", lambda vp_id: sent["registry"].append(vp_id)
    )
    return sent


def _use(monkeypatch, table):
    monkeypatch.setattr(index, "ddb", FakeDdb(table))
    return table


ROWS = [
    {"id": "a", "CallId": "m1"},
    {"id": "b", "CallId": "other"},
    {"id": "c", "CallId": "other"},
    {"id": "d", "CallId": "m1"},
    {"id": "e"},
    {"id": "f", "CallId": "m1"},
]


def test_every_record_of_the_meeting_is_deleted_across_scan_pages(monkeypatch, ended) -> None:
    table = _use(monkeypatch, FakeVPTable(ROWS, page=2))
    assert index.cleanup_virtual_participants("m1") == 3
    assert table.deleted == ["a", "d", "f"]
    assert table.scans == 3
    assert ended["end"] == ended["stop"] == ended["registry"] == ["a", "d", "f"]


def test_a_scan_page_with_no_match_does_not_end_the_search(monkeypatch, ended) -> None:
    rows = [{"id": f"x{n}", "CallId": "other"} for n in range(5)] + [{"id": "z", "CallId": "m1"}]
    table = _use(monkeypatch, FakeVPTable(rows, page=2))
    assert index.cleanup_virtual_participants("m1") == 1
    assert table.deleted == ["z"]


def test_a_record_that_cannot_be_deleted_fails_the_cleanup_after_the_rest(
    monkeypatch, ended
) -> None:
    table = _use(monkeypatch, FakeVPTable(ROWS, fail_delete={"d"}))
    with pytest.raises(RuntimeError, match="1 Virtual Participant record"):
        index.cleanup_virtual_participants("m1")
    assert table.deleted == ["a", "f"]
    # The registry entry of a record that is still there is kept with it.
    assert ended["registry"] == ["a", "f"]


def test_a_scan_that_fails_fails_the_cleanup(monkeypatch, ended) -> None:
    class FailingTable(FakeVPTable):
        def scan(self, **_kwargs):
            raise RuntimeError("scan failed")

    _use(monkeypatch, FailingTable([]))
    with pytest.raises(RuntimeError, match="scan failed"):
        index.cleanup_virtual_participants("m1")


def test_a_failed_cleanup_keeps_the_meeting_record(monkeypatch) -> None:
    """delete_meeting surfaces the failure and does not delete the meeting record."""
    executed = []

    def failing_cleanup(_callid):
        raise RuntimeError("could not delete 1 Virtual Participant record(s) of meeting m1")

    monkeypatch.setattr(index, "cleanup_virtual_participants", failing_cleanup)
    monkeypatch.setattr(index, "delete_transcript_items", lambda callid: 0)
    monkeypatch.setattr(index, "get_call_details", lambda *a: {"getCall": {}})
    monkeypatch.setattr(index, "delete_recordings_transcripts", lambda callid: None)
    monkeypatch.setattr(index, "delete_metering_records", lambda callid: 0)
    monkeypatch.setattr(index, "delete_keyframes", lambda callid: 0)
    monkeypatch.setattr(index, "delete_consent_evidence", lambda callid: 0)
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
        def execute(self, query):
            executed.append(query)
            return {}

    with pytest.raises(RuntimeError, match="Virtual Participant"):
        index.delete_meeting(_Session(), _Field(), "m1", "cls#x", "x", "owner@example.com")
    assert executed == []


def test_without_a_virtual_participant_table_nothing_is_read(monkeypatch) -> None:
    monkeypatch.setattr(index, "VP_TABLE_NAME", None)
    assert index.cleanup_virtual_participants("m1") == 0
