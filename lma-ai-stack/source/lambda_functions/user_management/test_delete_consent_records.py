# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Deleting a user, with and without erasing their consent records (no AWS).

Unticked, the consents the account gave in advance are withdrawn and kept, as
before. Ticked, they are erased by lma_consent.administration.erase: only those
that account gave (its sub), with the withdrawal passed to the meetings in
progress that counted them, and a failure is reported as such, since the
account is already gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_account_names as names  # noqa: E402  pylint: disable=wrong-import-position
from lma_consent import administration  # noqa: E402  pylint: disable=wrong-import-position

index = names.index
aws_fixture = names.aws_fixture
USER = names.USER


@pytest.fixture(name="calls")
def calls_fixture(aws, monkeypatch):
    calls = {"erase": [], "withdraw": [], "invoked": []}

    def erase(client, table, **kwargs):
        calls["erase"].append(kwargs)
        kwargs["pass_on"]({"ConsentId": "c1", "AppliedAt": {"Weekly": "2026-11-26T10:00:00Z"}})
        return {"Deleted": 3, "Withdrawn": 1, "ConsentIds": ["c1", "c2", "c3"], "AuditId": "a1"}

    monkeypatch.setattr(administration, "erase", erase)
    monkeypatch.setattr(index, "_withdraw_advance_consents", lambda *a: calls["withdraw"].append(a))
    monkeypatch.setattr(index, "MEETING_CONSENT_FUNCTION", "meeting-consent")

    class Lambda:
        def invoke(self, **kwargs):
            calls["invoked"].append(kwargs["FunctionName"])

    dynamo = aws.dynamo
    monkeypatch.setattr(
        index.boto3, "client", lambda name, *_a, **_k: Lambda() if name == "lambda" else dynamo
    )
    return calls


def delete(**extra):
    return index.handler(names.event("deleteUser", {"input": {"username": USER, **extra}}), None)


def test_by_default_a_deleted_users_consents_are_withdrawn_and_kept(calls) -> None:
    assert delete() == {"username": USER, "success": True}
    assert calls["withdraw"] == [(USER, f"sub-{USER}")]
    assert calls["erase"] == []


def test_ticked_a_deleted_users_consent_records_are_erased_after_live_meetings_are_told(
    calls,
) -> None:
    answer = delete(deleteConsentRecords=True)
    assert answer == {"username": USER, "success": True, "consentRecordsDeleted": 3}
    [erased] = calls["erase"]
    assert (erased["email"], erased["subject"], erased["reason"]) == (
        USER,
        f"sub-{USER}",
        "account_deleted",
    )
    assert erased["erased_by"] == names.ADMIN
    assert calls["invoked"] == ["meeting-consent"]
    assert calls["withdraw"] == []


def test_a_failed_erasure_says_the_user_was_deleted(calls, monkeypatch) -> None:
    def failing(*_a, **_k):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Query")

    monkeypatch.setattr(administration, "erase", failing)
    with pytest.raises(Exception, match="The user was deleted, but their consent records"):
        delete(deleteConsentRecords=True)
    # At least withdrawn, as without the option.
    assert calls["withdraw"] == [(USER, f"sub-{USER}")]


def test_an_erasure_with_more_remaining_runs_again_until_none_remain(calls, monkeypatch) -> None:
    answers = [
        {"Deleted": 2, "Withdrawn": 0, "ConsentIds": [], "AuditId": "a1", "MoreRemain": True},
        {"Deleted": 1, "Withdrawn": 0, "ConsentIds": [], "AuditId": "a2", "MoreRemain": False},
    ]
    monkeypatch.setattr(administration, "erase", lambda *_a, **_k: answers.pop(0))
    answer = delete(deleteConsentRecords=True)
    assert answer["consentRecordsDeleted"] == 3 and answers == []


def test_an_erasure_that_leaves_some_says_so_and_where_to_finish(calls, monkeypatch) -> None:
    rounds = []

    def erase(*_a, **_k):
        rounds.append(1)
        return {"Deleted": 2, "Withdrawn": 0, "ConsentIds": [], "AuditId": "a", "MoreRemain": True}

    monkeypatch.setattr(administration, "erase", erase)
    with pytest.raises(Exception, match="consent records were erased, but more remain"):
        delete(deleteConsentRecords=True)
    assert len(rounds) == index.MAX_ERASURE_ROUNDS


def test_consents_left_after_the_last_round_are_withdrawn(calls, monkeypatch) -> None:
    more = {"Deleted": 2, "Withdrawn": 0, "ConsentIds": [], "AuditId": "a", "MoreRemain": True}
    monkeypatch.setattr(administration, "erase", lambda *_a, **_k: dict(more))
    with pytest.raises(Exception, match="more remain"):
        delete(deleteConsentRecords=True)
    assert calls["withdraw"] == [(USER, f"sub-{USER}")]


def test_no_erasure_round_starts_once_its_time_is_spent(calls, monkeypatch) -> None:
    rounds = []

    def erase(*_a, **_k):
        rounds.append(1)
        return {"Deleted": 2, "Withdrawn": 0, "ConsentIds": [], "AuditId": "a", "MoreRemain": True}

    monkeypatch.setattr(administration, "erase", erase)
    monkeypatch.setattr(index, "ERASURE_SECONDS", -1.0)
    with pytest.raises(Exception, match="more remain"):
        delete(deleteConsentRecords=True)
    assert len(rounds) == 1


def test_a_round_that_fails_after_one_that_erased_says_how_many_were_erased(
    calls, monkeypatch
) -> None:
    answers = [{"Deleted": 2, "Withdrawn": 0, "ConsentIds": [], "AuditId": "a", "MoreRemain": True}]

    def erase(*_a, **_k):
        if answers:
            return answers.pop(0)
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Query")

    monkeypatch.setattr(administration, "erase", erase)
    with pytest.raises(Exception, match=r"could not all be erased \(2 were erased first\)"):
        delete(deleteConsentRecords=True)
