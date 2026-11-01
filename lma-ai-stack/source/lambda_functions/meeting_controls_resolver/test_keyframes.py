# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""A meeting's key frames are shared, unshared and deleted with the meeting.

Same setup as test_delete_metering_records.py: no AWS, the AppSync client and
the GraphQL DSL replaced before import, a fake table that pages its results.
"""

from __future__ import annotations

from test_delete_metering_records import FakeTable, index


class KeyframeTable(FakeTable):
    def __init__(self, keys, page=2):
        super().__init__(keys, page)
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


KEYS = [{"PK": "kf#m 1", "SK": f"f#{i:010d}-ab"} for i in range(3)]


def test_deleting_a_meeting_deletes_its_frames_and_its_image_folder(monkeypatch) -> None:
    table = KeyframeTable(KEYS)
    prefixes = []
    monkeypatch.setattr(index, "ddbTable", table)
    monkeypatch.setattr(
        index, "_batch_delete_prefix", lambda bucket, prefix: prefixes.append(prefix) or 6
    )
    assert index.delete_keyframes("m 1") == 3
    assert table.deleted == KEYS
    assert {q["ExpressionAttributeValues"][":pk"] for q in table.queries} == {"kf#m 1"}
    # The folder the producers write to, with a trailing slash so that meeting
    # "m 1" does not take "m 10"'s images with it.
    assert prefixes == ["lma-video-keyframes/m_1/"]


def test_sharing_a_meeting_shares_its_frames(monkeypatch) -> None:
    table = KeyframeTable(KEYS)
    monkeypatch.setattr(index, "ddbTable", table)
    assert index.share_keyframes("m 1", ["a@example.com", "b@example.com"]) == 3
    assert [u["Key"] for u in table.updates] == KEYS
    assert all(u["UpdateExpression"] == "SET SharedWith = :val" for u in table.updates)
    assert table.updates[0]["ExpressionAttributeValues"] == {
        ":val": ["a@example.com", "b@example.com"]
    }


def test_unsharing_a_meeting_removes_its_frames_sharing(monkeypatch) -> None:
    table = KeyframeTable(KEYS)
    monkeypatch.setattr(index, "ddbTable", table)
    index.share_keyframes("m 1", None)
    assert all(u["UpdateExpression"] == "REMOVE SharedWith" for u in table.updates)
    assert all("ExpressionAttributeValues" not in u for u in table.updates)


def test_a_meeting_without_frames_touches_nothing(monkeypatch) -> None:
    table = KeyframeTable([])
    monkeypatch.setattr(index, "ddbTable", table)
    monkeypatch.setattr(index, "_batch_delete_prefix", lambda bucket, prefix: 0)
    assert index.delete_keyframes("m-2") == 0
    assert index.share_keyframes("m-2", ["a@example.com"]) == 0
    assert table.deleted == [] and table.updates == []
