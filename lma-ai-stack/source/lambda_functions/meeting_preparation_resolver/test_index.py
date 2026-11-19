#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the meeting preparation resolver.

The rules themselves are tested in the configuration layer
(lma_meeting_preparation/test_rules.py). What is tested here is what a caller
depends on: who may write, that a signed-in user's change is theirs and a
status they set is a user's, that the assistant never overwrites it, that a
replace keeps progress, that concurrent checklist writes are both kept, that
quotes are not stored once the transcript is deleted, and the check the
Virtual Participant pipelines run.

No AWS: the module's client is built at import time and replaced here by a
fake that evaluates the condition expressions the resolver writes.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", "lma-call-events")

HERE = Path(__file__).resolve().parent


def _load():
    """Import this directory's index.py under a name of its own."""
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location(
        "meeting_preparation_resolver_index", HERE / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

CALL_ID = "Planning 2026-11-19"
OWNER = "owner@example.com"
SHARED = "shared@example.com"
OTHER = "other@example.com"
SER = TypeSerializer()
DES = TypeDeserializer()

BACKEND = {
    "userArn": "arn:aws:sts::123456789012:assumed-role/AnalyzerRole/session",
    "accountId": "1",
    "username": "AROAEXAMPLE:session",
}


def user(name: str, *, admin: bool = False) -> Dict[str, Any]:
    return {
        "username": name,
        "claims": {"sub": f"sub-{name}", "cognito:groups": ["Admin"] if admin else []},
    }


def _condition_holds(expression: str, item: Dict[str, Any], names: Dict, values: Dict) -> bool:
    """The condition forms the resolver writes, evaluated against a plain item."""

    def resolve(token: str):
        token = token.strip()
        if token.startswith("#"):
            return item.get(names[token])
        return DES.deserialize(values[token])

    def term(text: str) -> bool:
        text = text.strip()
        if text.startswith("(") and text.endswith(")"):
            return any(term(part) for part in text[1:-1].split(" OR "))
        match = re.fullmatch(r"attribute_exists\((\S+)\)", text)
        if match:
            name = match.group(1)
            return (names.get(name, name)) in item
        match = re.fullmatch(r"attribute_not_exists\((\S+)\)", text)
        if match:
            return names[match.group(1)] not in item
        match = re.fullmatch(r"contains\((\S+), (\S+)\)", text)
        if match:
            container = resolve(match.group(1))
            return isinstance(container, (list, set)) and resolve(match.group(2)) in container
        left, right = text.split(" = ")
        return resolve(left) == resolve(right)

    return all(term(part) for part in expression.split(" AND "))


class FakeDynamo:
    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.before_write = None
        self.updates = []

    def put(self, **attributes) -> None:
        self.items[f"c#{CALL_ID}"] = {
            "PK": f"c#{CALL_ID}",
            "SK": f"c#{CALL_ID}",
            "CallId": CALL_ID,
            **attributes,
        }

    @property
    def row(self) -> Dict[str, Any]:
        return self.items[f"c#{CALL_ID}"]

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["PK"]["S"])
        if item is None:
            return {}
        return {"Item": {key: SER.serialize(index._dynamo(value)) for key, value in item.items()}}

    def update_item(self, **kwargs):
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        key = kwargs["Key"]["PK"]["S"]
        item = self.items.get(key, {})
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        if not _condition_holds(kwargs["ConditionExpression"], item, names, values):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.updates.append(kwargs)
        expression = kwargs["UpdateExpression"]
        sets, _, removes = expression.partition(" REMOVE ")
        for part in sets[len("SET ") :].split(", "):
            name, value = part.split(" = ")
            item[names[name]] = index._plain(DES.deserialize(values[value]))
        for name in filter(None, removes.split(", ")):
            item.pop(names[name], None)
        self.items[key] = item
        return {"Attributes": {k: SER.serialize(index._dynamo(v)) for k, v in item.items()}}


@pytest.fixture
def table(monkeypatch):
    fake = FakeDynamo()
    fake.put(Owner=OWNER, SharedWith=[SHARED])
    monkeypatch.setattr(index, "DYNAMODB", fake)
    return fake


def call(field: str, identity: Dict[str, Any], **data):
    return index.handler(
        {
            "info": {"fieldName": field},
            "identity": identity,
            "arguments": {"input": {"CallId": CALL_ID, **data}},
        },
        None,
    )


ITEMS = [
    {"ItemId": "q1", "Text": "Who owns the budget?", "Kind": "question", "Priority": "must"},
    {"ItemId": "t1", "Text": "Hiring plan", "Kind": "topic"},
]


# ── who may write ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "identity",
    [user(OWNER), user(SHARED), user("admin@example.com", admin=True), BACKEND],
    ids=["owner", "shared", "admin", "backend"],
)
def test_the_owner_a_shared_user_an_admin_and_the_backend_may_write(table, identity):
    row = call("updateCallMeetingContext", identity, MeetingContext="# Goals\nAgree the budget")
    assert row["MeetingContext"] == "# Goals\nAgree the budget"


def test_anyone_else_is_refused(table):
    with pytest.raises(index.ForbiddenError):
        call("updateCallLabels", user(OTHER), Labels=[{"Key": "k", "Value": "v"}])
    assert "Labels" not in table.row


def test_a_meeting_shared_as_text_is_written_by_its_listed_users(table):
    table.put(Owner=OWNER, SharedWith=f"[a@example.com, {SHARED}]")
    assert call("updateCallAudience", user(SHARED), Audience="external")["Audience"] == "external"
    with pytest.raises(index.ForbiddenError):
        call("updateCallAudience", user("a@example.co"), Audience="internal")


def test_a_user_unshared_between_the_read_and_the_write_is_refused(table):
    table.before_write = lambda: table.row.update(SharedWith=[])
    with pytest.raises(index.ForbiddenError):
        call("updateCallMeetingContext", user(SHARED), MeetingContext="notes")
    assert "MeetingContext" not in table.row


def test_a_missing_meeting_is_refused_to_a_user_and_named_to_the_backend(table):
    table.items.clear()
    with pytest.raises(index.ForbiddenError):
        call("updateCallAudience", user(OWNER), Audience="internal")
    with pytest.raises(index.BadRequestError, match="no meeting"):
        call("updateCallAudience", BACKEND, Audience="internal")


def test_a_caller_with_no_identity_is_refused(table):
    with pytest.raises(index.ForbiddenError):
        call("updateCallAudience", {}, Audience="internal")


# ── context, labels, audience ───────────────────────────────────────────────


def test_a_user_s_context_change_is_attributed_to_them(table):
    row = call(
        "updateCallMeetingContext", user(SHARED), MeetingContext="ctx", UpdatedBy="someone else"
    )
    assert row["MeetingContextUpdatedBy"] == SHARED
    assert row["MeetingContextUpdatedAt"].endswith("Z")


def test_the_backend_names_who_it_acts_for(table):
    row = call(
        "updateCallMeetingContext", BACKEND, MeetingContext="ctx", UpdatedBy="assistant client"
    )
    assert row["MeetingContextUpdatedBy"] == "assistant client"
    assert (
        call("updateCallMeetingContext", BACKEND, MeetingContext="ctx")["MeetingContextUpdatedBy"]
        == "LMA"
    )


def test_an_over_long_context_is_refused_with_its_limit(table):
    with pytest.raises(index.BadRequestError, match="longer than 20000"):
        call("updateCallMeetingContext", user(OWNER), MeetingContext="x" * 20001)


def test_labels_are_replaced_and_cleared_to_an_empty_list(table):
    call("updateCallLabels", user(OWNER), Labels=[{"Key": "project", "Value": "website-relaunch"}])
    assert table.row["Labels"] == [{"Key": "project", "Value": "website-relaunch"}]
    row = call("updateCallLabels", user(OWNER), Labels=[])
    assert row["Labels"] == []
    assert row["MeetingMetadataUpdatedAt"]


def test_an_invalid_label_is_refused(table):
    with pytest.raises(index.BadRequestError, match="label Key"):
        call("updateCallLabels", user(OWNER), Labels=[{"Key": "Project", "Value": "x"}])


def test_the_audience_is_set_and_cleared(table):
    call("updateCallAudience", user(OWNER), Audience="internal")
    assert table.row["Audience"] == "internal"
    row = call("updateCallAudience", user(OWNER), Audience=None)
    assert "Audience" not in row and row["MeetingMetadataUpdatedAt"]
    with pytest.raises(index.BadRequestError, match="internal, external"):
        call("updateCallAudience", user(OWNER), Audience="public")


# ── the checklist ───────────────────────────────────────────────────────────


def test_a_checklist_is_set_with_every_item_open(table):
    row = call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    assert [item["Status"] for item in row["Checklist"]] == ["open", "open"]
    assert row["Checklist"][1]["Priority"] == "should"
    assert row["ChecklistVersion"] == 1 and row["ChecklistUpdatedAt"]


def test_a_replace_keeps_progress_and_a_reset_clears_it(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    call("updateCallChecklistItem", user(SHARED), ItemId="q1", Status="covered", Note="Dana")
    row = call("updateCallChecklist", user(OWNER), Checklist=[ITEMS[1], ITEMS[0]])
    q1 = row["Checklist"][1]
    assert (q1["ItemId"], q1["Status"], q1["StatusSource"], q1["Note"]) == (
        "q1",
        "covered",
        "user",
        "Dana",
    )
    row = call("updateCallChecklist", user(OWNER), Checklist=ITEMS, ResetStatuses=True)
    assert row["Checklist"][0]["Status"] == "open" and row["Checklist"][0]["Note"] is None


def test_a_signed_in_user_s_status_is_a_user_s_whatever_the_request_says(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    row = call(
        "updateCallChecklistItem",
        user(OWNER),
        ItemId="q1",
        Status="partial",
        StatusSource="assistant",
    )
    assert row["Checklist"][0]["StatusSource"] == "user"
    assert row["Checklist"][0]["UpdatedBy"] == OWNER


def test_the_assistant_never_overwrites_a_status_a_user_set(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    call("updateCallChecklistItem", user(OWNER), ItemId="q1", Status="deferred")
    with pytest.raises(index.StatusSetByUserError):
        call(
            "updateCallChecklistItem",
            BACKEND,
            ItemId="q1",
            Status="covered",
            StatusSource="assistant",
        )
    assert table.row["Checklist"][0]["Status"] == "deferred"
    # A backend role acting for a user (an MCP client, say) is a user.
    row = call(
        "updateCallChecklistItem", BACKEND, ItemId="q1", Status="covered", StatusSource="user"
    )
    assert row["Checklist"][0]["Status"] == "covered"


def test_the_assistant_sets_a_status_with_evidence(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    row = call(
        "updateCallChecklistItem",
        BACKEND,
        ItemId="t1",
        Status="partial",
        StatusSource="assistant",
        AddEvidence=[{"SegmentId": "seg-9", "StartTime": 125.5, "Quote": "we hire two"}],
    )
    item = row["Checklist"][1]
    assert item["StatusSource"] == "assistant"
    assert item["Evidence"] == [{"SegmentId": "seg-9", "StartTime": 125.5, "Quote": "we hire two"}]
    assert item["UpdatedBy"] == "LMA"


def test_a_backend_item_update_must_name_its_source(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    with pytest.raises(index.BadRequestError, match="StatusSource"):
        call("updateCallChecklistItem", BACKEND, ItemId="q1", Status="covered")


def test_no_quote_is_stored_once_the_transcript_is_deleted(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)
    table.row["TranscriptDeletedAt"] = "2026-11-19T12:00:00Z"
    row = call(
        "updateCallChecklistItem",
        BACKEND,
        ItemId="q1",
        StatusSource="assistant",
        AddEvidence=[{"SegmentId": "s", "StartTime": 3, "Quote": "transcript words"}],
    )
    assert row["Checklist"][0]["Evidence"] == [{"SegmentId": "s", "StartTime": 3, "Quote": None}]


def test_a_checklist_changed_between_the_read_and_the_write_is_read_again(table):
    call("updateCallChecklist", user(OWNER), Checklist=ITEMS)

    def concurrent():
        items = [dict(item) for item in table.row["Checklist"]]
        items[1].update(Status="covered", StatusSource="assistant")
        table.row.update(Checklist=items, ChecklistVersion=table.row["ChecklistVersion"] + 1)

    table.before_write = concurrent
    row = call("updateCallChecklistItem", user(OWNER), ItemId="q1", Status="covered")
    assert [item["Status"] for item in row["Checklist"]] == ["covered", "covered"]
    assert row["ChecklistVersion"] == 3


def test_an_invalid_checklist_is_refused_with_the_rule(table):
    with pytest.raises(index.BadRequestError, match="at most 100 items"):
        call(
            "updateCallChecklist",
            user(OWNER),
            Checklist=[{**ITEMS[0], "ItemId": f"i{i}"} for i in range(101)],
        )
    with pytest.raises(index.BadRequestError, match="no item"):
        call("updateCallChecklistItem", user(OWNER), ItemId="missing", Status="covered")


def test_an_unknown_field_is_refused(table):
    with pytest.raises(index.BadRequestError, match="unsupported"):
        call("deleteCall", user(OWNER))


# ── the Virtual Participant pipelines ───────────────────────────────────────


def validate(present, **data):
    return index.handler(
        {
            "info": {"fieldName": "validateMeetingPreparation"},
            "arguments": {"input": data, "present": present},
        },
        None,
    )


def test_the_vp_check_normalizes_the_four_inputs():
    out = validate(
        ["labels", "audience", "meetingContext", "checklist"],
        labels=[{"Key": "project", "Value": "acme"}],
        audience="external",
        meetingContext="Goals",
        checklist=ITEMS,
    )
    assert out["present"] == ["labels", "audience", "meetingContext", "checklist"]
    assert out["labels"] == [{"Key": "project", "Value": "acme"}]
    assert out["audience"] == "external" and out["meetingContext"] == "Goals"
    assert out["checklist"][1] == {
        "ItemId": "t1",
        "Text": "Hiring plan",
        "Kind": "topic",
        "Priority": "should",
    }


def test_the_vp_check_reports_an_empty_input_as_one_to_remove():
    out = validate(
        ["labels", "audience", "meetingContext", "checklist"],
        labels=[],
        audience="",
        meetingContext="",
        checklist=[],
    )
    assert out == {
        "present": ["labels", "audience", "meetingContext", "checklist"],
        "labels": None,
        "audience": None,
        "meetingContext": None,
        "checklist": None,
    }


def test_the_vp_check_leaves_out_what_was_not_sent():
    assert validate([], audience="external") == {"present": []}


def test_the_vp_check_refuses_an_invalid_input():
    with pytest.raises(index.BadRequestError, match="Kind must be one of"):
        validate(["checklist"], checklist=[{"ItemId": "a", "Text": "x", "Kind": "idea"}])
