#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting whiteboards function (no AWS).

What a caller depends on: who may read and change a meeting's whiteboards; that
a write is judged against the meeting and the version at the moment it is made;
that a user's panel is never changed by the assistant, including a panel a user
takes while a run is in progress; the limits; versions and restore; and that a
run makes no model call unless the meeting has the assistant on and enough was
said. The table is a fake that evaluates the conditions the function writes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", "lma-call-events")

HERE = Path(__file__).resolve().parent


def _load():
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("meeting_whiteboards_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()
from lma_whiteboards import rules  # noqa: E402  pylint: disable=wrong-import-position

SER = TypeSerializer()
DES = TypeDeserializer()
CALL = "call-1"
OWNER = "owner@example.com"
SHARED = "shared@example.com"
OTHER = "other@example.com"
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/Role/session", "username": "AROA:s"}


def user(name: str, admin: bool = False) -> Dict[str, Any]:
    return {"username": name, "claims": {"sub": name, "cognito:groups": ["Admin"] if admin else []}}


def _fail(reasons: List[str] | None = None) -> None:
    if reasons is None:
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "Write")
    raise ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": r} for r in reasons],
        },
        "TransactWriteItems",
    )


class FakeTable:
    """Rows by (PK, SK), with the conditions the function writes evaluated."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Dict[str, Any]] = {}

    @staticmethod
    def _k(key: Dict[str, Any]) -> tuple:
        return key["PK"]["S"], key["SK"]["S"]

    def put_plain(self, row: Dict[str, Any]) -> None:
        self.rows[(row["PK"], row["SK"])] = dict(row)

    def plain(self, pk: str, sk: str) -> Dict[str, Any] | None:
        return self.rows.get((pk, sk))

    def _serialized(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {k: SER.serialize(index._dynamo(v)) for k, v in row.items()}

    # ── reads ──
    def get_item(self, **kwargs: Any) -> Dict[str, Any]:
        row = self.rows.get(self._k(kwargs["Key"]))
        return {"Item": self._serialized(row)} if row else {}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]["S"]
        prefix = values.get(":prefix", {}).get("S", "")
        rows = sorted(
            (r for (p, s), r in self.rows.items() if p == pk and s.startswith(prefix)),
            key=lambda r: r["SK"],
            reverse=kwargs.get("ScanIndexForward") is False,
        )
        start = kwargs.get("ExclusiveStartKey")
        if start:
            sks = [r["SK"] for r in rows]
            rows = rows[sks.index(start["SK"]["S"]) + 1 :]
        limit = kwargs.get("Limit")
        page = rows[:limit] if limit else rows
        out: Dict[str, Any] = {"Items": [self._serialized(r) for r in page]}
        if limit and len(rows) > limit:
            out["LastEvaluatedKey"] = {"PK": {"S": pk}, "SK": {"S": page[-1]["SK"]}}
        return out

    # ── conditions ──
    def _meeting_ok(self, row, names, values) -> bool:
        if row is None or "TranscriptDeletedAt" in row:
            return False
        caller = values.get(":accessCaller", {}).get("S")
        if caller is None:
            return True
        if row.get("Owner") == caller:
            return True
        if ":accessSharedAsRead" in values:
            return row.get("SharedWith") == values[":accessSharedAsRead"]["S"]
        shared = row.get("SharedWith")
        return isinstance(shared, list) and caller in shared

    def _holds(self, key: tuple, condition: str, names, values) -> bool:
        row = self.rows.get(key)
        if key[0].startswith("c#"):
            return self._meeting_ok(row, names, values)
        if condition == "attribute_not_exists(PK)":
            return row is None
        if condition == "attribute_exists(PK)":
            return row is not None
        if condition == "#v = :base":
            return row is not None and row.get("Version") == int(values[":base"]["N"])
        if "Boards < :max" in condition:
            return row is None or row.get("Boards", 0) < int(values[":max"]["N"])
        if "Boards > :zero" in condition:
            return row is not None and row.get("Boards", 0) > 0
        if "#c < :max" in condition:
            return row is None or row.get("Count", 0) < int(values[":max"]["N"])
        raise AssertionError(condition)

    def _apply_update(self, key: tuple, expression: str, names, values) -> Dict[str, Any]:
        row = self.rows.setdefault(key, {"PK": key[0], "SK": key[1]})
        if expression.startswith("ADD Boards"):
            row["Boards"] = row.get("Boards", 0) + int(
                values[":one" if ":one" in values else ":minus"]["N"]
            )
        elif expression.startswith("ADD #c"):
            row["Count"] = row.get("Count", 0) + 1
        elif expression == "SET #w = :w":
            row["WhiteboardsEnabled"] = values[":w"]["BOOL"]
        if "ExpiresAfter" in expression and ":ttl" in values:
            row.setdefault("ExpiresAfter", int(values[":ttl"]["N"]))
        return row

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        key = self._k(kwargs["Key"])
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        if not self._holds(key, kwargs.get("ConditionExpression", ""), names, values):
            _fail()
        row = self._apply_update(key, kwargs["UpdateExpression"], names, values)
        return {"Attributes": self._serialized(row)}

    def transact_write_items(self, TransactItems: List[Dict[str, Any]]) -> None:  # noqa: N803
        reasons = []
        for item in TransactItems:
            ((kind, body),) = item.items()
            key = (
                self._k(body["Key"])
                if "Key" in body
                else (body["Item"]["PK"]["S"], body["Item"]["SK"]["S"])
            )
            ok = self._holds(
                key,
                body.get("ConditionExpression", ""),
                body.get("ExpressionAttributeNames", {}),
                body.get("ExpressionAttributeValues", {}),
            )
            reasons.append("None" if ok else "ConditionalCheckFailed")
        if any(r != "None" for r in reasons):
            _fail(reasons)
        for item in TransactItems:
            ((kind, body),) = item.items()
            if kind == "Put":
                row = {k: index._plain(DES.deserialize(v)) for k, v in body["Item"].items()}
                self.rows[(row["PK"], row["SK"])] = row
            elif kind == "Delete":
                self.rows.pop(self._k(body["Key"]), None)
            elif kind == "Update":
                self._apply_update(
                    self._k(body["Key"]),
                    body["UpdateExpression"],
                    body.get("ExpressionAttributeNames", {}),
                    body.get("ExpressionAttributeValues", {}),
                )

    def batch_write_item(self, RequestItems: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N803
        for requests in RequestItems.values():
            for request in requests:
                self.rows.pop(self._k(request["DeleteRequest"]["Key"]), None)
        return {}


@pytest.fixture
def table(monkeypatch) -> FakeTable:
    fake = FakeTable()
    fake.put_plain(
        {
            "PK": f"c#{CALL}",
            "SK": f"c#{CALL}",
            "CallId": CALL,
            "Owner": OWNER,
            "SharedWith": [SHARED],
            "Status": "STARTED",
            "ExpiresAfter": 2_000_000_000,
        }
    )
    monkeypatch.setattr(index, "DYNAMODB", fake)
    monkeypatch.setattr(index, "transcript_expires_after", lambda: 1_900_000_000)
    monkeypatch.setattr(index, "load_settings", lambda: rules.Settings())
    return fake


def call(field: str, identity: Dict[str, Any], **arguments: Any) -> Any:
    return index.handler(
        {"info": {"fieldName": field}, "identity": identity, "arguments": arguments}, None
    )


def create(identity=None, **data) -> Dict[str, Any]:
    return call("createMeetingWhiteboard", identity or user(OWNER), input={"CallId": CALL, **data})


def update(
    board: Dict[str, Any], panels: List[Dict[str, Any]], identity=None, **data
) -> Dict[str, Any]:
    return call(
        "updateMeetingWhiteboard",
        identity or user(OWNER),
        input={
            "CallId": CALL,
            "WhiteboardId": board["WhiteboardId"],
            "BaseVersion": board["Version"],
            "Panels": panels,
            **data,
        },
    )


def panels_of(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{k: p[k] for k in ("PanelId", "Title", "Kind", "Content")} for p in board["Panels"]]


def refused(error_type: str):
    return pytest.raises(index.ERRORS[error_type])


# ── access ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("identity", [user(OWNER), user(SHARED), user(OTHER, admin=True)])
def test_the_owner_a_shared_user_and_an_admin_make_and_read_whiteboards(table, identity) -> None:
    board = create(identity, Title="Plan")
    assert board["Owner"] == OWNER and board["SharedWith"] == [SHARED]
    listed = call("listMeetingWhiteboards", identity, CallId=CALL)
    assert [b["WhiteboardId"] for b in listed["Items"]] == [board["WhiteboardId"]]
    assert listed["Writable"] is True and listed["MaxWhiteboards"] == 5


def test_anyone_else_neither_reads_nor_writes_them(table) -> None:
    create()
    with refused("Unauthorized"):
        call("listMeetingWhiteboards", user(OTHER), CallId=CALL)
    with refused("Unauthorized"):
        create(user(OTHER))


def test_a_meeting_that_does_not_exist_gets_the_same_answer_as_one_not_shared(table) -> None:
    with refused("Unauthorized"):
        call("listMeetingWhiteboards", user(OWNER), CallId="nope")


def test_a_write_is_judged_on_the_meeting_as_it_is_when_written(table, monkeypatch) -> None:
    board = create(user(SHARED))
    real = index.read_meeting

    def unshared_after_read(call_id, **kwargs):
        meeting = real(call_id, **kwargs)
        table.rows[(f"c#{CALL}", f"c#{CALL}")]["SharedWith"] = []
        return meeting

    monkeypatch.setattr(index, "read_meeting", unshared_after_read)
    panels = panels_of(board)
    panels[0]["Content"] = "- decided"
    with refused("Unauthorized"):
        update(board, panels, user(SHARED))


def test_a_signed_in_users_change_is_always_their_own(table) -> None:
    board = create(user(SHARED), Source="assistant", UpdatedBy="someone-else")
    assert board["CreatedBy"] == SHARED
    panels = panels_of(board)
    panels[0]["Content"] = "mine"
    changed = update(board, panels, user(SHARED), Source="assistant")
    assert changed["Panels"][0]["Source"] == "user" and changed["Panels"][0]["UpdatedBy"] == SHARED


def test_a_backend_change_names_its_source(table) -> None:
    with refused("BadRequest"):
        create(BACKEND)


# ── versions and concurrency ─────────────────────────────────────────────────


def test_each_change_is_a_new_version_and_the_old_one_is_kept(table) -> None:
    board = create(Template="decisions")
    panels = panels_of(board)
    panels[0]["Content"] = "- Ship in March"
    v2 = update(board, panels)
    assert v2["Version"] == 2
    versions = call(
        "listMeetingWhiteboardVersions",
        user(OWNER),
        CallId=CALL,
        WhiteboardId=board["WhiteboardId"],
    )
    assert [v["Version"] for v in versions["Items"]] == [2, 1]
    assert (
        versions["Items"][0]["ChangedPanels"] == ["decisions"]
        and versions["Items"][0]["Source"] == "user"
    )
    v1 = call(
        "getMeetingWhiteboard",
        user(OWNER),
        CallId=CALL,
        WhiteboardId=board["WhiteboardId"],
        Version=1,
    )
    assert v1["Panels"][0]["Content"] == ""


def test_a_change_made_from_an_older_version_is_refused(table) -> None:
    board = create()
    panels = panels_of(board)
    panels[0]["Content"] = "first"
    update(board, panels)
    panels[0]["Content"] = "second"
    with refused("WhiteboardConflict"):
        update(board, panels)


def test_a_change_that_changes_nothing_makes_no_version(table) -> None:
    board = create()
    same = update(board, panels_of(board))
    assert same["Version"] == 1
    assert len([k for k in table.rows if k[1].startswith("v#")]) == 1


def test_every_version_expires_with_its_whiteboard(table, monkeypatch) -> None:
    board = create()
    # Later in the transcript's retention, the transcript's expiry is later.
    monkeypatch.setattr(index, "transcript_expires_after", lambda: 1_950_000_000)
    panels = panels_of(board)
    panels[0]["Content"] = "- decided"
    update(board, panels)
    head = table.plain(f"wb#{CALL}", f"b#{board['WhiteboardId']}")
    versions = [r for (pk, sk), r in table.rows.items() if sk.startswith("v#")]
    assert head["ExpiresAfter"] == 1_900_000_000
    assert {v["ExpiresAfter"] for v in versions} == {1_900_000_000}


def test_the_count_of_whiteboards_outlives_every_whiteboard_it_counts(table, monkeypatch) -> None:
    create()
    # A whiteboard made later in the transcript's retention expires later.
    monkeypatch.setattr(index, "transcript_expires_after", lambda: 1_950_000_000)
    later = create()
    counter = table.plain(f"wb#{CALL}", "index")
    heads = [r for (pk, sk), r in table.rows.items() if sk.startswith("b#")]
    assert counter["ExpiresAfter"] >= max(h["ExpiresAfter"] for h in heads)
    # It takes the meeting record's expiry, which caps every whiteboard's.
    assert counter["ExpiresAfter"] == 2_000_000_000
    gone = call(
        "deleteMeetingWhiteboard",
        user(OWNER),
        input={"CallId": CALL, "WhiteboardId": later["WhiteboardId"]},
    )
    assert gone["Deleted"] is True


def test_restoring_a_version_makes_a_new_one(table) -> None:
    board = create()
    panels = panels_of(board)
    panels[0]["Content"] = "later"
    v2 = update(board, panels)
    restored = call(
        "updateMeetingWhiteboard",
        user(OWNER),
        input={
            "CallId": CALL,
            "WhiteboardId": board["WhiteboardId"],
            "BaseVersion": 2,
            "RestoreVersion": 1,
        },
    )
    assert restored["Version"] == 3 and restored["Panels"][0]["Content"] == ""
    assert v2["Panels"][0]["Content"] == "later"


def test_version_pages_follow_their_token_and_refuse_a_foreign_one(table) -> None:
    board = create()
    for n in range(3):
        panels = panels_of(board)
        panels[0]["Content"] = f"v{n}"
        board = update(board, panels)
    first = call(
        "listMeetingWhiteboardVersions",
        user(OWNER),
        CallId=CALL,
        WhiteboardId=board["WhiteboardId"],
        Limit=2,
    )
    assert [v["Version"] for v in first["Items"]] == [4, 3] and first["NextToken"]
    second = call(
        "listMeetingWhiteboardVersions",
        user(OWNER),
        CallId=CALL,
        WhiteboardId=board["WhiteboardId"],
        Limit=2,
        NextToken=first["NextToken"],
    )
    assert [v["Version"] for v in second["Items"]] == [2, 1]
    other = index._encode_token({"PK": {"S": "wb#other"}, "SK": {"S": "v#x#000001"}})
    with refused("BadRequest"):
        call(
            "listMeetingWhiteboardVersions",
            user(OWNER),
            CallId=CALL,
            WhiteboardId=board["WhiteboardId"],
            NextToken=other,
        )


# ── the assistant and the user ───────────────────────────────────────────────


def test_the_assistant_never_changes_a_panel_a_user_wrote(table) -> None:
    board = create()
    panels = panels_of(board)
    panels[0]["Content"] = "the user's decision"
    board = update(board, panels)
    panels = panels_of(board)
    panels[0]["Content"] = "the assistant's rewrite"
    with refused("WhiteboardUserPanel"):
        update(board, panels, BACKEND, Source="assistant")
    assert (
        table.plain(f"wb#{CALL}", f"b#{board['WhiteboardId']}")["Panels"][0]["Content"]
        == "the user's decision"
    )


def test_a_whiteboard_with_assistant_updates_off_is_not_changed_by_it(table) -> None:
    board = create(AssistantUpdates=False)
    panels = panels_of(board)
    panels[0]["Content"] = "x"
    with refused("WhiteboardAssistantOff"):
        update(board, panels, BACKEND, Source="assistant")


# ── limits and deletion ──────────────────────────────────────────────────────


def test_a_meeting_has_at_most_its_whiteboards_and_a_deletion_frees_one(table) -> None:
    boards = [create() for _ in range(5)]
    with refused("WhiteboardLimit"):
        create()
    gone = call(
        "deleteMeetingWhiteboard",
        user(OWNER),
        input={"CallId": CALL, "WhiteboardId": boards[0]["WhiteboardId"]},
    )
    assert gone["Deleted"] is True and gone["Owner"] == OWNER
    assert not [k for k in table.rows if k[1].startswith(f"v#{boards[0]['WhiteboardId']}#")]
    create()


def test_version_rows_left_unprocessed_are_logged_with_their_count(
    table, monkeypatch, caplog
) -> None:
    """The deletion is committed; the version rows DynamoDB leaves unprocessed
    through every attempt stay until their expiry, and a log line says how many."""
    board = create()
    panels = panels_of(board)
    panels[0]["Content"] = "later"
    update(board, panels)
    prefix = f"v#{board['WhiteboardId']}#"
    versions = [k for k in table.rows if k[1].startswith(prefix)]
    assert len(versions) == 2
    attempts = []

    def throttled(RequestItems):  # noqa: N803
        attempts.append(RequestItems)
        return {"UnprocessedItems": RequestItems}

    monkeypatch.setattr(table, "batch_write_item", throttled)
    with caplog.at_level("WARNING"):
        gone = call(
            "deleteMeetingWhiteboard",
            user(OWNER),
            input={"CallId": CALL, "WhiteboardId": board["WhiteboardId"]},
        )
    assert gone["Deleted"] is True
    assert len(attempts) == 5
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(f"2 of 2 whiteboard rows under {prefix} of {CALL}" in w for w in warnings), warnings


def test_deleting_every_version_row_returns_none_remaining(table) -> None:
    board = create()
    assert index.delete_rows(CALL, f"v#{board['WhiteboardId']}#") == 0
    assert not [k for k in table.rows if k[1].startswith(f"v#{board['WhiteboardId']}#")]


def test_changes_are_limited_per_minute(table, monkeypatch) -> None:
    monkeypatch.setattr(index, "WRITES_PER_MINUTE", 2)
    create()
    create()
    with refused("WhiteboardRateLimited"):
        create()


def test_nothing_is_written_once_the_transcript_is_deleted(table) -> None:
    board = create()
    table.rows[(f"c#{CALL}", f"c#{CALL}")]["TranscriptDeletedAt"] = "2026-11-20T11:00:00Z"
    with refused("WhiteboardTranscriptDeleted"):
        update(board, panels_of(board))
    listed = call("listMeetingWhiteboards", user(OWNER), CallId=CALL)
    assert listed["Items"] == [] and listed["Writable"] is False


def test_every_row_that_holds_content_takes_the_transcripts_expiry(table) -> None:
    create()
    # The write counters and the count of whiteboards hold no meeting content;
    # the count expires with the meeting record (see the test below).
    rows = [
        r
        for (pk, _), r in table.rows.items()
        if pk == f"wb#{CALL}" and not r["SK"].startswith("r#") and r["SK"] != "index"
    ]
    assert rows and {r.get("ExpiresAfter") for r in rows} == {1_900_000_000}


def test_the_meeting_switch_is_written_on_the_meeting_row(table) -> None:
    call_row = call(
        "updateCallWhiteboards", user(SHARED), input={"CallId": CALL, "WhiteboardsEnabled": True}
    )
    assert call_row["WhiteboardsEnabled"] is True and call_row["Owner"] == OWNER
    with refused("Unauthorized"):
        call(
            "updateCallWhiteboards",
            user(OTHER),
            input={"CallId": CALL, "WhiteboardsEnabled": False},
        )


# ── the assistant's run ──────────────────────────────────────────────────────


class FakeLease:
    def __init__(self) -> None:
        self.claims: List[Dict[str, Any]] = []
        self.releases: List[Dict[str, Any]] = []
        self.state: Dict[str, Any] = {}

    def claim(self, call_id, **kwargs):
        self.claims.append(kwargs)
        return {**self.state, "LeaseUntil": 1}

    def release(self, call_id, lease, **kwargs):
        self.releases.append(kwargs)
        # What a run leaves is what the next one reads, as on the state row.
        self.state.update({k: v for k, v in kwargs.items() if k not in ("ran", "now")})


def _segments(table: FakeTable, count: int, start: float = 0.0) -> None:
    for n in range(count):
        table.put_plain(
            {
                "PK": f"trs#{CALL}",
                "SK": f"s{start + n:08.1f}",
                "SegmentId": f"seg{start + n}",
                "Channel": "CALLER",
                "IsPartial": False,
                "StartTime": start + n,
                "EndTime": start + n + 0.9,
                "Transcript": f"We decided item {n}.",
            }
        )


@pytest.fixture
def running(table, monkeypatch):
    lease = FakeLease()
    monkeypatch.setattr(index, "LIVE", lease)
    monkeypatch.setattr(index, "model_for", lambda settings: "global.anthropic.claude-haiku-5-5")
    monkeypatch.setattr(index, "read_setting", lambda *a, **k: k.get("default"))

    def appsync(query, variables):
        field = (
            "updateMeetingWhiteboard"
            if "updateMeetingWhiteboard" in query
            else "createMeetingWhiteboard"
        )
        try:
            result = call(field, BACKEND, input=variables["input"])
        except index.ApiError as error:
            raise index.NotificationError(str(error), error.error_type) from error
        return {field: result}

    monkeypatch.setattr(index, "appsync_graphql", appsync)
    calls: List[Dict[str, Any]] = []
    answers: List[Any] = []

    class Bedrock:
        def converse(self, **request):
            calls.append(request)
            answer = answers.pop(0) if answers else {"whiteboards": []}
            if callable(answer):
                answer = answer()
            return {
                "output": {
                    "message": {
                        "content": [{"toolUse": {"name": "update_whiteboards", "input": answer}}]
                    }
                },
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }

    monkeypatch.setattr(index, "bedrock", lambda: Bedrock())
    table.rows[(f"c#{CALL}", f"c#{CALL}")]["WhiteboardsEnabled"] = True
    return {"lease": lease, "calls": calls, "answers": answers}


def test_with_the_assistant_off_for_the_meeting_no_run_happens(table, running) -> None:
    table.rows[(f"c#{CALL}", f"c#{CALL}")]["WhiteboardsEnabled"] = False
    _segments(table, 20)
    assert index.handler({"CallId": CALL, "Trigger": "auto"}, None)["ran"] is False
    assert running["calls"] == [] and running["lease"].claims == []


def test_a_run_waits_for_enough_new_transcript(table, running) -> None:
    _segments(table, 5)
    result = index.run(CALL)
    assert result["ran"] is False and running["calls"] == []
    assert running["lease"].releases == [
        {"ran": False, "now": pytest.approx(running["lease"].releases[0]["now"])}
    ]


def test_a_run_makes_the_default_whiteboard_and_fills_it(table, running) -> None:
    _segments(table, 20)

    def answer():
        board = index.read_boards(CALL)[0]
        return {
            "whiteboards": [
                {
                    "whiteboardId": board["WhiteboardId"],
                    "panels": [{"panelId": "decisions", "content": "- Item 0"}],
                }
            ],
            "notes": "decided items",
        }

    running["answers"].append(answer)
    result = index.run(CALL)
    assert result == {"ran": True, "updated": 1, "dropped": 0}
    board = index.read_boards(CALL)[0]
    assert board["Template"] == "decisions" and board["CreatedBy"] == rules.ASSISTANT_LABEL
    assert (
        board["Panels"][0]["Content"] == "- Item 0" and board["Panels"][0]["Source"] == "assistant"
    )
    release = running["lease"].releases[-1]
    assert release["ran"] is True and release["Notes"] == "decided items"
    assert release["LastSegmentEndTime"] == pytest.approx(19.9)
    request = running["calls"][0]
    assert "We decided item 19." in request["messages"][0]["content"][-1]["text"]


def test_a_segment_finalized_late_is_read_by_the_next_run_once(table, running) -> None:
    _segments(table, 20)
    running["answers"].append({"whiteboards": [], "notes": ""})
    index.run(CALL)
    marker = running["lease"].state["LastSegmentEndTime"]
    assert json.loads(running["lease"].state["ShownSegmentIds"])
    # Finalized after the first run read the transcript, though it ended before its marker.
    table.put_plain(
        {
            "PK": f"trs#{CALL}",
            "SK": "s00000015.5",
            "SegmentId": "finalized-late",
            "Channel": "AGENT",
            "IsPartial": False,
            "StartTime": marker - 5,
            "EndTime": marker - 1,
            "Transcript": "A long point that was finalized late.",
        }
    )
    _segments(table, 20, start=100.0)
    running["answers"].append({"whiteboards": [], "notes": ""})
    index.run(CALL)
    second = running["calls"][1]["messages"][0]["content"][-1]["text"]
    assert "A long point that was finalized late." in second
    _segments(table, 20, start=200.0)
    running["answers"].append({"whiteboards": [], "notes": ""})
    index.run(CALL)
    third = running["calls"][2]["messages"][0]["content"][-1]["text"]
    assert "A long point that was finalized late." not in third


def test_a_panel_a_user_takes_during_a_run_keeps_their_content(table, running) -> None:
    board = create()
    _segments(table, 20)

    def answer():
        # The user edits Decisions while the model is answering.
        current = index.read_boards(CALL)[0]
        panels = panels_of(current)
        panels[0]["Content"] = "the user's decision"
        update(current, panels)
        return {
            "whiteboards": [
                {
                    "whiteboardId": board["WhiteboardId"],
                    "panels": [
                        {"panelId": "decisions", "content": "the assistant's decision"},
                        {"panelId": "notes", "content": "- a note"},
                    ],
                }
            ]
        }

    running["answers"].append(answer)
    assert index.run(CALL)["updated"] == 1
    stored = index.read_boards(CALL)[0]
    assert stored["Panels"][0]["Content"] == "the user's decision"
    assert stored["Panels"][0]["Source"] == "user"
    assert stored["Panels"][2]["Content"] == "- a note"


def test_update_now_runs_after_a_short_gap_and_only_with_the_meeting_switch_on(
    table, running
) -> None:
    create()
    _segments(table, 2)
    table.rows[(f"c#{CALL}", f"c#{CALL}")]["WhiteboardsEnabled"] = False
    off = call("requestWhiteboardUpdate", user(SHARED), CallId=CALL)
    assert off["Success"] is False and running["calls"] == []
    assert running["lease"].claims == []
    table.rows[(f"c#{CALL}", f"c#{CALL}")]["WhiteboardsEnabled"] = True
    result = call("requestWhiteboardUpdate", user(SHARED), CallId=CALL)
    assert result["Success"] is True and len(running["calls"]) == 1
    # Not the interval, but a short gap, so repeated presses cannot use up the runs.
    assert running["lease"].claims[0]["ignore_interval"] is False
    assert running["lease"].claims[0]["interval_seconds"] == index.REQUEST_GAP_SECONDS
    with refused("Unauthorized"):
        call("requestWhiteboardUpdate", user(OTHER), CallId=CALL)


def test_no_run_once_the_meeting_has_ended_except_the_last_one(table, running) -> None:
    table.rows[(f"c#{CALL}", f"c#{CALL}")]["Status"] = "ENDED"
    create()
    _segments(table, 20)
    assert index.run(CALL, "auto")["ran"] is False
    assert index.run(CALL, "final")["ran"] is True


def test_the_default_whiteboard_is_made_once_and_not_again_after_it_is_deleted(
    table, running
) -> None:
    _segments(table, 20)
    assert index.run(CALL)["ran"] is True
    board = index.read_boards(CALL)[0]
    call(
        "deleteMeetingWhiteboard",
        user(OWNER),
        input={"CallId": CALL, "WhiteboardId": board["WhiteboardId"]},
    )
    _segments(table, 20, start=100.0)
    result = index.run(CALL)
    assert result == {"ran": False, "reason": "no whiteboard the assistant updates"}
    assert index.read_boards(CALL) == []


def test_deleting_a_whiteboard_counts_as_a_change(table, monkeypatch) -> None:
    monkeypatch.setattr(index, "WRITES_PER_MINUTE", 1)
    board = create()
    with refused("WhiteboardRateLimited"):
        call(
            "deleteMeetingWhiteboard",
            user(OWNER),
            input={"CallId": CALL, "WhiteboardId": board["WhiteboardId"]},
        )


def test_a_malformed_answer_changes_nothing_and_counts_the_run(table, running) -> None:
    create()
    _segments(table, 20)
    running["answers"].append({"whiteboards": "not a list"})
    result = index.run(CALL)
    assert result["reason"] == "malformed answer"
    assert running["lease"].releases[-1]["ran"] is True
    assert index.read_boards(CALL)[0]["Version"] == 1
