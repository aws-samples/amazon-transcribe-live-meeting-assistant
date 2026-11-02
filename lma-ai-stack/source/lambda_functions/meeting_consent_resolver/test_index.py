#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the meeting consent resolver.

The rules themselves are tested in the configuration layer
(lma_consent/test_rules.py). What is tested here is what a caller depends on:
who may do each thing, that a meeting's consent cannot be weakened past what
the deployment allows, that concurrent writes do not lose a reply, that the
answer carries what the subscription filter needs, and that evidence links
are signed only inside the meeting's own folder.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", "lma-call-events")
os.environ.setdefault("RECORDINGS_BUCKET", "lma-recordings")

HERE = Path(__file__).resolve().parent


def _load():
    """Import this directory's index.py under a name of its own."""
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location(
        "meeting_consent_resolver_index", HERE / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

CALL_ID = "Weekly sync 2026-11-02"
OWNER = "owner@example.com"
SHARED = "shared@example.com"
OTHER = "other@example.com"
SER = TypeSerializer()
DES = TypeDeserializer()


def user(name: str, *, admin: bool = False) -> Dict[str, Any]:
    groups = ["Admin"] if admin else []
    return {"username": name, "claims": {"sub": f"sub-{name}", "cognito:groups": groups}}


# As AppSync passes an IAM identity: the username is the role id and session.
BACKEND = {
    "userArn": "arn:aws:sts::123456789012:assumed-role/VPTaskRole/session",
    "accountId": "1",
    "username": "AROAEXAMPLE:session",
}


class FakeDynamo:
    """The meeting row, with the version condition the resolver writes on."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.conflicts = 0
        self.updates = 0

    def put_meeting(self, call_id: str = CALL_ID, **fields: Any) -> None:
        row = {"PK": f"c#{call_id}", "SK": f"c#{call_id}", "CallId": call_id, "Owner": OWNER}
        row.update(fields)
        self.items[call_id] = {k: SER.serialize(v) for k, v in row.items()}

    def consent(self, call_id: str = CALL_ID) -> Any:
        item = self.items[call_id]
        return index._plain(DES.deserialize(item["Consent"])) if "Consent" in item else None

    def get_item(self, TableName, Key, **_kwargs):  # noqa: N803
        call_id = Key["PK"]["S"][2:]
        item = self.items.get(call_id)
        return {"Item": dict(item)} if item else {}

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression, **kwargs):  # noqa: N803
        self.updates += 1
        call_id = Key["PK"]["S"][2:]
        item = self.items.get(call_id)
        failed = item is None
        if item is not None:
            if self.conflicts:
                self.conflicts -= 1
                failed = True
            elif "attribute_not_exists(#c)" in ConditionExpression:
                failed = "Consent" in item
            else:
                stored = DES.deserialize(item["Consent"]) if "Consent" in item else {}
                failed = (
                    str(stored.get("Version")) != kwargs["ExpressionAttributeValues"][":v"]["N"]
                )
        if failed:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
                "UpdateItem",
            )
        assert "ReturnValues" not in kwargs  # the answer is built from the row as read
        item["Consent"] = kwargs["ExpressionAttributeValues"][":c"]
        return {}


class FakeStore:
    def __init__(self, values=None, fail=False) -> None:
        self.values = values or {}
        self.fail = fail

    def get_section(self, name):
        assert name == "meeting_consent"
        if self.fail:
            raise ClientError({"Error": {"Code": "Throttling", "Message": "slow"}}, "GetItem")
        return SimpleNamespace(values=self.values)


class FakeS3:
    def generate_presigned_url(self, _operation, Params, ExpiresIn):  # noqa: N803
        return f"https://signed/{Params['Key']}?e={ExpiresIn}"


@pytest.fixture
def dynamo(monkeypatch):
    fake = FakeDynamo()
    fake.put_meeting(SharedWith=[SHARED])
    monkeypatch.setattr(index, "DYNAMODB", fake)
    monkeypatch.setattr(index, "S3", FakeS3())
    monkeypatch.setattr(index, "_STORE", FakeStore())
    return fake


def settings(monkeypatch, **values):
    monkeypatch.setattr(index, "_STORE", FakeStore(values))


def call(field, identity, **arguments):
    if field != "getMeetingConsent":
        arguments = {"input": {"CallId": CALL_ID, **arguments}}
    else:
        arguments = {"CallId": CALL_ID, **arguments}
    return index.handler(
        {"info": {"fieldName": field}, "arguments": arguments, "identity": identity}, None
    )


# ── reading ────────────────────────────────────────────────────────────────


def test_a_meeting_without_a_record_is_gated_by_the_deployment_mode(dynamo, monkeypatch) -> None:
    result = call("getMeetingConsent", user(OWNER))
    assert result["Consent"] is None and result["CaptureAllowed"] is True
    settings(monkeypatch, mode="opt_in")
    result = call("getMeetingConsent", user(SHARED))
    assert result["CaptureAllowed"] is False
    assert result["Settings"]["Mode"] == "opt_in"
    assert result["Settings"]["ConsentPhrases"][0] == "I consent"


def test_somebody_without_access_cannot_read_it(dynamo) -> None:
    with pytest.raises(index.ForbiddenError):
        call("getMeetingConsent", user(OTHER))


def test_an_unknown_meeting_is_refused_like_one_without_access(dynamo) -> None:
    with pytest.raises(index.ForbiddenError):
        index.handler(
            {
                "info": {"fieldName": "getMeetingConsent"},
                "arguments": {"CallId": "missing"},
                "identity": user(OTHER),
            },
            None,
        )


def test_unreadable_settings_refuse_rather_than_default_to_off(dynamo, monkeypatch) -> None:
    monkeypatch.setattr(index, "_STORE", FakeStore(fail=True))
    with pytest.raises(index.ConfigurationUnavailableError):
        call("startMeetingConsent", BACKEND)
    assert dynamo.consent() is None


# ── starting ───────────────────────────────────────────────────────────────


def test_starting_uses_the_deployment_default_and_renders_the_notice(dynamo, monkeypatch) -> None:
    settings(monkeypatch, mode="opt_in", consentRule="owner_attests")
    result = call("startMeetingConsent", BACKEND, LmaUser="Ana", MeetingName="Weekly sync")
    record = result["Consent"]
    assert (record["Mode"], record["Rule"], record["Status"]) == (
        "opt_in",
        "owner_attests",
        "pending",
    )
    assert record["CaptureAllowed"] is False
    assert record["NoticeText"].startswith("Ana would like to record")
    assert record["Version"] == 1 and record["StartedBy"] == "LMA"
    assert result["Owner"] == OWNER and result["SharedWith"] == [SHARED]


def test_a_shared_user_cannot_start_or_change_it(dynamo) -> None:
    with pytest.raises(index.ForbiddenError):
        call("startMeetingConsent", user(SHARED), Mode="off")


def test_without_overrides_a_meeting_cannot_use_less_consent(dynamo, monkeypatch) -> None:
    settings(monkeypatch, mode="opt_in", allowPerMeetingOverride=False)
    with pytest.raises(index.BadRequestError, match="less consent"):
        call("startMeetingConsent", user(OWNER), Mode="notify")
    with pytest.raises(index.BadRequestError, match="own notice"):
        call("startMeetingConsent", user(OWNER), NoticeText="Hello")
    assert call("startMeetingConsent", user(OWNER))["Consent"]["Mode"] == "opt_in"


def test_with_overrides_a_meeting_may_choose_its_mode(dynamo, monkeypatch) -> None:
    settings(monkeypatch, mode="opt_in")
    record = call(
        "startMeetingConsent", user(OWNER), Mode="notify", NoticeText="{LMA_USER} records"
    )["Consent"]
    assert (record["Mode"], record["Rule"], record["NoticeText"]) == (
        "notify",
        None,
        "The meeting organizer records",
    )


def test_starting_again_is_idempotent_but_cannot_switch_once_under_way(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="opt_in", Rule="each_participant")
    assert (
        call("startMeetingConsent", BACKEND, Mode="opt_in", Rule="each_participant")["Consent"][
            "Mode"
        ]
        == "opt_in"
    )
    call(
        "recordMeetingConsentEvent",
        BACKEND,
        ParticipantName="Bo",
        Decision="objection",
        Source="chat",
    )
    with pytest.raises(index.BadRequestError, match="already under way"):
        call("startMeetingConsent", BACKEND, Mode="off")


def test_writes_before_starting_are_refused(dynamo) -> None:
    with pytest.raises(index.BadRequestError, match="not been started"):
        call("recordMeetingConsentNotice", BACKEND, Channel="chat")


# ── the opt-in flow ────────────────────────────────────────────────────────


def test_each_participant_flow_end_to_end(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="opt_in", Rule="each_participant")
    call("recordMeetingConsentNotice", BACKEND, Channel="chat")
    call("setMeetingConsentParticipants", BACKEND, Participants=["Ana", " ana ", "Bo"])
    assert dynamo.consent()["Participants"] == ["Ana", "Bo"]
    call(
        "recordMeetingConsentEvent",
        BACKEND,
        ParticipantName="Ana",
        Decision="consent",
        Source="chat",
        Text="I consent",
    )
    assert dynamo.consent()["CaptureAllowed"] is False
    result = call(
        "recordMeetingConsentEvent",
        BACKEND,
        ParticipantName="BO",
        Decision="consent",
        Source="voice",
    )
    assert (result["Consent"]["Status"], result["Consent"]["CaptureAllowed"]) == ("granted", True)
    assert call("getMeetingConsent", user(OWNER))["CaptureAllowed"] is True
    history = [(h["Status"], h["CaptureAllowed"]) for h in result["Consent"]["StatusHistory"]]
    assert history == [("pending", False), ("granted", True)]
    # Somebody who joins later withholds capture again.
    call("setMeetingConsentParticipants", BACKEND, Participants=["Ana", "Bo", "Cy"])
    assert dynamo.consent()["CaptureAllowed"] is False
    assert dynamo.consent()["ParticipantsSeen"] == ["Ana", "Bo", "Cy"]


def test_a_shared_user_cannot_change_who_is_present(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="opt_in", Rule="each_participant")
    with pytest.raises(index.ForbiddenError):
        call("setMeetingConsentParticipants", user(SHARED), Participants=[])


def test_a_shared_user_may_record_an_objection_but_not_consent(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    with pytest.raises(index.ForbiddenError):
        call("recordMeetingConsentEvent", user(SHARED), ParticipantName="Bo", Decision="consent")
    result = call(
        "recordMeetingConsentEvent",
        user(SHARED),
        ParticipantName="Bo",
        Decision="objection",
        Source="chat",
    )
    event = result["Consent"]["Events"][-1]
    assert (event["Source"], event["RecordedBy"]) == ("ui", SHARED)
    assert result["Consent"]["Status"] == "objected"


def test_a_backend_reply_names_where_it_was_read(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    with pytest.raises(index.BadRequestError, match="Source"):
        call(
            "recordMeetingConsentEvent",
            BACKEND,
            ParticipantName="Bo",
            Decision="consent",
            Source="ui",
        )


def test_only_the_owner_or_the_backend_attests(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="opt_in", Rule="owner_attests")
    for identity in (user(SHARED), user("admin@example.com", admin=True)):
        with pytest.raises(index.ForbiddenError):
            call("attestMeetingConsent", identity, Method="verbal")
    with pytest.raises(index.BadRequestError, match="AttestedBy"):
        call("attestMeetingConsent", BACKEND, Method="verbal")
    result = call(
        "attestMeetingConsent", user(OWNER), Method="invite_terms", Statement="In the invite"
    )
    record = result["Consent"]
    assert record["Attestation"]["AttestedBy"] == OWNER
    assert (record["Status"], record["CaptureAllowed"]) == ("granted", True)
    assert record["Events"][-1]["Source"] == "attestation"


def test_an_objection_after_the_attestation_stops_capture(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="opt_in", Rule="owner_attests")
    call("attestMeetingConsent", BACKEND, Method="chat", AttestedBy="Ana (owner)")
    result = call(
        "recordMeetingConsentEvent",
        BACKEND,
        ParticipantName="Bo",
        Decision="objection",
        Source="chat",
    )
    assert (result["Consent"]["Status"], result["Consent"]["CaptureAllowed"]) == ("objected", False)


# ── concurrency ────────────────────────────────────────────────────────────


def test_a_conflicting_write_is_retried_and_nothing_is_lost(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    dynamo.conflicts = 2
    call(
        "recordMeetingConsentEvent",
        BACKEND,
        ParticipantName="Bo",
        Decision="objection",
        Source="chat",
    )
    assert [e["ParticipantName"] for e in dynamo.consent()["Events"]] == ["Bo"]
    assert dynamo.consent()["Version"] == 2


def test_a_write_that_keeps_conflicting_is_reported(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    dynamo.conflicts = index.WRITE_ATTEMPTS
    with pytest.raises(index.ConflictError):
        call("recordMeetingConsentNotice", BACKEND, Channel="chat")


# ── evidence ───────────────────────────────────────────────────────────────


def test_evidence_is_added_by_the_backend_inside_the_meetings_folder(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    folder = "lma-consent-evidence/Weekly_sync_2026_11_02/"
    with pytest.raises(index.ForbiddenError):
        call("addMeetingConsentEvidence", user(OWNER), S3Key=f"{folder}a.jpg")
    for key in ("lma-video-keyframes/x.jpg", f"{folder}../other/a.jpg"):
        with pytest.raises(index.BadRequestError):
            call("addMeetingConsentEvidence", BACKEND, S3Key=key)
    call("addMeetingConsentEvidence", BACKEND, S3Key=f"{folder}chat.jpg", Note="chat panel")
    evidence = call("getMeetingConsent", user(SHARED))["Consent"]["Evidence"]
    assert evidence[0]["SignedUrl"] == f"https://signed/{folder}chat.jpg?e=3600"
    assert evidence[0]["Kind"] == "screenshot"


def test_a_stored_key_outside_the_folder_is_not_signed(dynamo) -> None:
    assert index.sign("lma-video-keyframes/x.jpg", CALL_ID) is None
    assert index.sign(None, CALL_ID) is None


def test_mutation_answers_carry_no_signed_links(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    folder = "lma-consent-evidence/Weekly_sync_2026_11_02/"
    result = call("addMeetingConsentEvidence", BACKEND, S3Key=f"{folder}chat.jpg")
    assert result["Consent"]["Evidence"][0]["SignedUrl"] is None


def test_an_unsupported_field_is_refused(dynamo) -> None:
    with pytest.raises(index.BadRequestError):
        index.handler(
            {"info": {"fieldName": "deleteCall"}, "arguments": {}, "identity": BACKEND}, None
        )


def test_starting_again_with_the_same_choice_writes_nothing(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    writes = dynamo.updates
    result = call("startMeetingConsent", BACKEND, Mode="notify")
    assert dynamo.updates == writes
    assert result["Consent"]["Version"] == 1


def test_evidence_also_holds_the_mode_in_place(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    call(
        "addMeetingConsentEvidence",
        BACKEND,
        S3Key="lma-consent-evidence/Weekly_sync_2026_11_02/a.jpg",
    )
    with pytest.raises(index.BadRequestError, match="already under way"):
        call("startMeetingConsent", BACKEND, Mode="off")


def test_a_shared_user_cannot_record_a_notice(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    with pytest.raises(index.ForbiddenError):
        call("recordMeetingConsentNotice", user(SHARED), Channel="chat")
    result = call("recordMeetingConsentNotice", user(OWNER), Channel="chat")
    assert result["Consent"]["Notices"][0]["PostedBy"] == OWNER
    assert result["Consent"]["Status"] == "granted"


def test_a_restart_keeps_its_mode_after_the_settings_change(dynamo, monkeypatch) -> None:
    settings(monkeypatch, mode="notify")
    call("startMeetingConsent", BACKEND)
    call("recordMeetingConsentNotice", BACKEND, Channel="chat")
    writes = dynamo.updates
    settings(monkeypatch, mode="opt_in", consentRule="owner_attests")
    result = call("startMeetingConsent", BACKEND)
    assert result["Consent"]["Mode"] == "notify"
    assert dynamo.updates == writes


def test_changing_the_mode_before_anything_was_posted_keeps_the_history(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    call("setMeetingConsentParticipants", BACKEND, Participants=["Ana"])
    result = call("startMeetingConsent", user(OWNER), Mode="opt_in", Rule="each_participant")
    record = result["Consent"]
    assert (record["Mode"], record["Status"], record["CaptureAllowed"]) == (
        "opt_in",
        "pending",
        False,
    )
    assert record["Participants"] == ["Ana"] and record["StartedBy"] == "LMA"
    assert record["StatusHistory"][-1]["Reason"] == (
        f"the mode was changed to opt_in (each_participant) by {OWNER}"
    )
    assert [h["Status"] for h in record["StatusHistory"]] == ["pending", "pending"]


def test_a_started_record_is_read_without_the_settings(dynamo, monkeypatch) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    monkeypatch.setattr(index, "_STORE", FakeStore(fail=True))
    result = call("getMeetingConsent", user(OWNER))
    assert result["CaptureAllowed"] is True and result["Settings"] is None
    dynamo.put_meeting(SharedWith=[SHARED])
    with pytest.raises(index.ConfigurationUnavailableError):
        call("getMeetingConsent", user(OWNER))


def test_a_backend_write_is_attributed_to_lma_not_its_session(dynamo) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    result = call("recordMeetingConsentNotice", BACKEND, Channel="chat")
    assert result["Consent"]["StartedBy"] == "LMA"
    assert result["Consent"]["Notices"][0]["PostedBy"] == "LMA"


def test_a_restart_needs_no_settings(dynamo, monkeypatch) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    monkeypatch.setattr(index, "_STORE", FakeStore(fail=True))
    assert call("startMeetingConsent", BACKEND)["Consent"]["Mode"] == "notify"
    with pytest.raises(index.ConfigurationUnavailableError):
        call("startMeetingConsent", BACKEND, Mode="opt_in")


def test_a_record_that_cannot_be_kept_within_its_budget_is_refused(dynamo, monkeypatch) -> None:
    call("startMeetingConsent", BACKEND, Mode="notify")
    monkeypatch.setattr(index.consent, "MAX_RECORD_BYTES", 10)
    monkeypatch.setattr(index.consent.fit_to_budget, "__defaults__", (10,))
    with pytest.raises(index.BadRequestError, match="would exceed"):
        call("recordMeetingConsentNotice", BACKEND, Channel="chat")
