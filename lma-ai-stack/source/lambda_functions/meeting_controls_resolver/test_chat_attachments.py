# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's chat attachments are shared, unshared and deleted with the meeting.

Same setup as test_keyframes.py: no AWS, the AppSync client and the GraphQL DSL
replaced before import, a fake table that pages its results.
"""

from __future__ import annotations

from test_delete_metering_records import FakeTable, index


class AttachmentTable(FakeTable):
    def __init__(self, keys, page=2):
        super().__init__(keys, page)
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


KEYS = [{"PK": "att#m 1", "SK": f"a#{i:010d}-ab"} for i in range(3)]


def test_deleting_a_meeting_deletes_its_attachments_and_their_folder(monkeypatch) -> None:
    table = AttachmentTable(KEYS)
    prefixes = []
    monkeypatch.setattr(index, "ddbTable", table)
    monkeypatch.setattr(
        index, "_batch_delete_prefix", lambda bucket, prefix: prefixes.append(prefix) or 4
    )
    assert index.delete_chat_attachments("m 1") == 3
    assert table.deleted == KEYS
    assert {q["ExpressionAttributeValues"][":pk"] for q in table.queries} == {"att#m 1"}
    # With a trailing slash, so that meeting "m 1" does not take "m 10"'s files.
    assert prefixes == ["lma-chat-attachments/m_1/"]


def test_the_meetings_deletion_includes_its_attachments() -> None:
    source = index.delete_meeting.__code__.co_names
    assert "delete_chat_attachments" in source


def test_sharing_a_meeting_shares_its_attachments(monkeypatch) -> None:
    table = AttachmentTable(KEYS)
    monkeypatch.setattr(index, "ddbTable", table)
    assert index.share_chat_attachments("m 1", ["a@example.com"]) == 3
    assert [u["Key"] for u in table.updates] == KEYS
    assert all(u["UpdateExpression"] == "SET SharedWith = :val" for u in table.updates)


def test_unsharing_a_meeting_removes_its_attachments_sharing(monkeypatch) -> None:
    table = AttachmentTable(KEYS)
    monkeypatch.setattr(index, "ddbTable", table)
    index.share_chat_attachments("m 1", None)
    assert all(u["UpdateExpression"] == "REMOVE SharedWith" for u in table.updates)


def test_a_meeting_without_attachments_touches_nothing(monkeypatch) -> None:
    table = AttachmentTable([])
    monkeypatch.setattr(index, "ddbTable", table)
    monkeypatch.setattr(index, "_batch_delete_prefix", lambda bucket, prefix: 0)
    assert index.delete_chat_attachments("m-2") == 0
    assert index.share_chat_attachments("m-2", ["a@example.com"]) == 0
    assert table.deleted == [] and table.updates == []
