# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for inviting somebody to enrol their voice.

An invitation is not a consent. It creates the ability for somebody to decide
for themselves, and records who asked them to -- so what matters here is that
only an admin can invite, that an invitation grants no access beyond the portal,
that it never stands in for the consent itself, and that a half-created account
is cleaned up rather than left able to reach nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("USER_POOL_ID", "us-east-1_test")
os.environ.setdefault("VOICE_CONSENT_LOG_TABLE", "consent-log")
os.environ.setdefault("VOICE_ENROLLMENT_TERMS_VERSION", "1")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


invite_mod = _load("voice_invite", "invite.py")

ADMIN = "sub-admin"
INVITEE_EMAIL = "someone@example.com"
INVITEE_SUB = "sub-invitee"


def _client_error(code="InternalFailure"):
    return ClientError({"Error": {"Code": code, "Message": "no"}}, "Op")


class FakeCognito:
    class exceptions:  # noqa: N801
        class UsernameExistsException(Exception):
            pass

    def __init__(self, exists=False, fail_group=False):
        self.exists = exists
        self.fail_group = fail_group
        self.created = []
        self.grouped = []
        self.deleted = []

    def admin_create_user(self, **kwargs):
        if self.exists:
            raise self.exceptions.UsernameExistsException()
        self.created.append(kwargs)
        return {"User": {"Attributes": [{"Name": "sub", "Value": INVITEE_SUB}]}}

    def admin_get_user(self, **kwargs):
        return {"UserAttributes": [{"Name": "sub", "Value": INVITEE_SUB}]}

    def admin_add_user_to_group(self, **kwargs):
        if self.fail_group:
            raise _client_error()
        self.grouped.append(kwargs)

    def admin_delete_user(self, **kwargs):
        self.deleted.append(kwargs)


class FakeDynamo:
    def __init__(self):
        self.puts = []

    def put_item(self, **kwargs):
        self.puts.append(kwargs)

    def update_item(self, **kwargs):  # pragma: no cover
        raise AssertionError("the consent log must never be updated")

    def delete_item(self, **kwargs):  # pragma: no cover
        raise AssertionError("the consent log must never be deleted from")


def install(monkeypatch, **kwargs):
    cognito = FakeCognito(**kwargs)
    ddb = FakeDynamo()
    monkeypatch.setattr(invite_mod, "cognito", cognito)
    monkeypatch.setattr(invite_mod, "dynamodb", ddb)
    return cognito, ddb


def event(email=INVITEE_EMAIL, sub=ADMIN, groups=("Admin",)):
    return {
        "arguments": {"input": {"Email": email}},
        "identity": {
            "claims": {"sub": sub, "email": f"{sub}@x.com", "cognito:groups": list(groups)}
        },
    }


def test_an_admin_can_invite_somebody(monkeypatch: pytest.MonkeyPatch) -> None:
    cognito, ddb = install(monkeypatch)
    result = invite_mod.handler(event(), None)
    assert result["Success"] is True
    assert cognito.created[0]["Username"] == INVITEE_EMAIL
    assert ddb.puts[0]["Item"]["Action"]["S"] == "INVITED"


def test_an_invitee_lands_in_the_enrollee_group_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """That group reaches the enrollment portal and nothing else, so an
    invitation cannot hand somebody meeting access by the back door."""
    cognito, _ = install(monkeypatch)
    invite_mod.handler(event(), None)
    assert [g["GroupName"] for g in cognito.grouped] == ["VoiceEnrollee"]


def test_a_non_admin_cannot_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    cognito, ddb = install(monkeypatch)
    result = invite_mod.handler(event(sub="sub-user", groups=()), None)
    assert result["Success"] is False
    assert cognito.created == []
    assert ddb.puts == []


def test_an_anonymous_caller_cannot_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    cognito, _ = install(monkeypatch)
    assert (
        invite_mod.handler({"arguments": {"input": {"Email": INVITEE_EMAIL}}}, None)["Success"]
        is False
    )
    assert cognito.created == []


def test_an_invitation_is_recorded_as_an_invitation_not_a_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It creates the ability to decide. Only the invitee can grant."""
    _, ddb = install(monkeypatch)
    invite_mod.handler(event(), None)
    row = ddb.puts[0]["Item"]
    assert row["Action"]["S"] == "INVITED"
    assert row["SubjectId"]["S"] == INVITEE_SUB
    assert row["ActorSub"]["S"] == ADMIN
    assert "Centroid" not in row


def test_the_invitation_row_is_append_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _, ddb = install(monkeypatch)
    invite_mod.handler(event(), None)
    assert "attribute_not_exists" in ddb.puts[0]["ConditionExpression"]


def test_inviting_somebody_who_already_has_an_account_still_records_the_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Common and fine: an LMA user may well want to enrol their own voice."""
    cognito, ddb = install(monkeypatch, exists=True)
    result = invite_mod.handler(event(), None)
    assert result["Success"] is True
    assert cognito.created == []
    assert ddb.puts[0]["Item"]["SubjectId"]["S"] == INVITEE_SUB


def test_a_half_created_account_is_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """An account in no group can reach nothing and is just litter."""
    cognito, ddb = install(monkeypatch, fail_group=True)
    result = invite_mod.handler(event(), None)
    assert result["Success"] is False
    assert cognito.deleted[0]["Username"] == INVITEE_EMAIL
    assert ddb.puts == []


def test_a_pre_existing_account_is_never_deleted_by_a_failed_invitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is not ours to delete -- it may be a real LMA user."""
    cognito, _ = install(monkeypatch, exists=True, fail_group=True)
    assert invite_mod.handler(event(), None)["Success"] is False
    assert cognito.deleted == []


def test_a_malformed_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    cognito, _ = install(monkeypatch)
    for bad in ("", "   ", "nope", "no@domain", "two@@at.com", "sp ace@x.com"):
        result = invite_mod.handler(event(email=bad), None)
        assert result["Success"] is False, bad
    assert cognito.created == []


def test_the_deployments_allowed_domain_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invitation must not be a way around the sign-up domain restriction."""
    cognito, _ = install(monkeypatch)
    monkeypatch.setattr(invite_mod, "ALLOWED_SIGNUP_EMAIL_DOMAIN", "example.com")
    assert invite_mod.handler(event(email="someone@elsewhere.com"), None)["Success"] is False
    assert cognito.created == []
    assert invite_mod.handler(event(email="someone@example.com"), None)["Success"] is True


def test_a_cognito_failure_is_reported_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cognito, _ = install(monkeypatch)

    def explode(**kwargs):
        raise _client_error("ThrottlingException")

    cognito.admin_create_user = explode
    result = invite_mod.handler(event(), None)
    assert result["Success"] is False
    assert "Throttling" not in result["Message"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
