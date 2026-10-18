# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the voice-enrollment retention pass.

The promise being kept here is that a consent lapses rather than standing for
ever, and that when it does the voiceprint is actually destroyed. So the cases
that matter are: only lapsed consents are touched, the biometric is gone
afterwards, the log records why, and one bad row does not stop the rest.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("VOICE_ENROLLMENT_TABLE", "enrollments")
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


# index is imported by retention for the shared serialisation helpers, so it has
# to be loaded under exactly that name.
index = _load("index", "index.py")
retention = _load("voice_retention", "retention.py")


def iso(days_from_now):
    return (
        (datetime.now(timezone.utc) + timedelta(days=days_from_now))
        .isoformat()
        .replace("+00:00", "Z")
    )


def row(subject, expires_in_days, status="active", with_print=True):
    item = {
        "SubjectId": {"S": subject},
        "DisplayName": {"S": "Sarah Lee"},
        "Status": {"S": status},
        "TermsVersion": {"S": "1"},
        "ConsentExpiresAt": {"S": iso(expires_in_days)},
    }
    if with_print:
        item["Centroid"] = {"L": [{"N": "0.6"}, {"N": "0.8"}]}
        item["Dim"] = {"N": "2"}
    return item


class FakeDynamo:
    class exceptions:  # noqa: N801
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, items, pages=None, fail_on=None):
        self.items = items
        self.pages = pages
        self.fail_on = fail_on or set()
        self.puts = []
        self.scans = 0

    def scan(self, **kwargs):
        self.scans += 1
        if self.pages is None:
            return {"Items": list(self.items)}
        start = 0
        if "ExclusiveStartKey" in kwargs:
            resume = kwargs["ExclusiveStartKey"]["SubjectId"]["S"]
            start = next(i for i, r in enumerate(self.items) if r["SubjectId"]["S"] == resume) + 1
        page = self.items[start : start + self.pages]
        out = {"Items": page}
        if start + self.pages < len(self.items):
            out["LastEvaluatedKey"] = {"SubjectId": page[-1]["SubjectId"]}
        return out

    def put_item(self, **kwargs):
        subject = kwargs["Item"]["SubjectId"]["S"]
        if subject in self.fail_on and kwargs["TableName"] == "enrollments":
            raise RuntimeError("write failed")
        self.puts.append(kwargs)

    def enrollment_writes(self):
        return [p["Item"] for p in self.puts if p["TableName"] == "enrollments"]

    def log_writes(self):
        return [p["Item"] for p in self.puts if p["TableName"] == "consent-log"]


def install(monkeypatch, items, **kwargs):
    ddb = FakeDynamo(items, **kwargs)
    monkeypatch.setattr(retention, "dynamodb", ddb)
    monkeypatch.setattr(index, "dynamodb", ddb)
    return ddb


def test_a_lapsed_consent_has_its_voiceprint_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, [row("sub-old", -1)])
    result = retention.handler({}, None)
    assert result["Expired"] == 1
    written = ddb.enrollment_writes()[0]
    assert "Centroid" not in written
    assert "Dim" not in written
    assert written["Status"]["S"] == "revoked"


def test_expiring_appends_to_the_consent_log(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch, [row("sub-old", -1)])
    retention.handler({}, None)
    logged = ddb.log_writes()[0]
    assert logged["Action"]["S"] == "EXPIRED"
    assert logged["SubjectId"]["S"] == "sub-old"
    # Not a person: a log row must never be ambiguous about whether somebody acted.
    assert logged["ActorSub"]["S"] == "system:retention"
    assert "Centroid" not in logged


def test_every_log_write_is_append_only(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch, [row("sub-old", -1)])
    retention.handler({}, None)
    log_puts = [p for p in ddb.puts if p["TableName"] == "consent-log"]
    assert all("attribute_not_exists" in p["ConditionExpression"] for p in log_puts)


def test_a_consent_still_in_force_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch, [row("sub-fresh", 100)])
    assert retention.handler({}, None)["Expired"] == 0
    assert ddb.puts == []


def test_an_already_revoked_enrollment_is_not_expired_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, [row("sub-gone", -5, status="revoked", with_print=False)])
    assert retention.handler({}, None)["Expired"] == 0
    assert ddb.puts == []


def test_the_voiceprint_is_destroyed_before_the_log_row_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the log write fails afterwards the data is still gone, which is the
    right way round -- the alternative leaves a live voiceprint with a row
    saying it was deleted."""
    ddb = install(monkeypatch, [row("sub-old", -1)])
    retention.handler({}, None)
    tables = [p["TableName"] for p in ddb.puts]
    assert tables.index("enrollments") < tables.index("consent-log")


def test_one_unwritable_row_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(
        monkeypatch,
        [row("sub-bad", -1), row("sub-good", -1)],
        fail_on={"sub-bad"},
    )
    result = retention.handler({}, None)
    assert result["Expired"] == 1
    assert result["Failed"] == 1
    assert [i["SubjectId"]["S"] for i in ddb.enrollment_writes()] == ["sub-good"]


def test_every_page_of_enrollments_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [row(f"sub-{i}", -1) for i in range(5)]
    ddb = install(monkeypatch, items, pages=2)
    assert retention.handler({}, None)["Expired"] == 5
    assert ddb.scans == 3


def test_the_table_is_read_once_not_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scanning twice would cost double and risk acting on a row that changed
    between the passes."""
    ddb = install(monkeypatch, [row("sub-old", -1)])
    retention.handler({}, None)
    assert ddb.scans == 1


def test_a_run_with_nothing_enrolled_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch, [])
    assert retention.handler({}, None) == {"Enrollments": 0, "Expired": 0, "Failed": 0}
    assert ddb.puts == []


def test_expiry_cannot_be_requested_through_the_api() -> None:
    """It is the one action may_grant allows nobody to perform, so neither an
    admin nor the enrollee can trigger it -- only this scheduled pass."""
    for actor, groups in (("sub-a", []), ("sub-admin", ["Admin"]), ("", [])):
        assert index.pure.may_grant(index.pure.ACTION_EXPIRED, "sub-a", actor, groups) is False
