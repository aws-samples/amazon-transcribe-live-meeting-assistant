# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the seeds a starting transcription session is given.

Two promises are being kept, and they pull in opposite directions, so both
need asserting.

The first is to the person who enrolled: their voiceprint is used only while all
four conditions of their consent hold, and it never travels further than it has
to. So the tests check that a withdrawal, a terms change and a lapsed consent
each stop it being sent, and that no subject id leaves this function.

The second is to the meeting: naming is an enhancement, and a transcript is not.
So the tests check that everything which can go wrong here -- an unreadable
table, a row with no voiceprint, more enrollments than fit -- yields fewer names
rather than a failed call.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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


# seeds imports index for the shared deserialisation, so index must be loaded
# under exactly that name.
index = _load("index", "index.py")
pure = _load("pure", "pure.py")
seeds = _load("voice_seeds", "seeds.py")


def iso(days_from_now):
    return (
        (datetime.now(timezone.utc) + timedelta(days=days_from_now))
        .isoformat()
        .replace("+00:00", "Z")
    )


def row(
    subject,
    *,
    name="Sarah Lee",
    status="active",
    terms="1",
    expires_in_days=30,
    enrolled_at=None,
    centroid=(0.1, 0.2, 0.3),
):
    """One enrollment as DynamoDB returns it.

    Decimals rather than floats, because that is what the table actually hands
    back and the engine needs plain numbers -- a test using floats here would
    pass while the real call shipped something the wire format cannot carry.
    """
    item = {
        "SubjectId": {"S": subject},
        "DisplayName": {"S": name},
        "Status": {"S": status},
        "TermsVersion": {"S": terms},
        "EnrolledAt": {"S": enrolled_at or iso(-1)},
        "ConsentExpiresAt": {"S": iso(expires_in_days)},
    }
    if centroid is not None:
        item["Centroid"] = {"L": [{"N": str(Decimal(str(c)))} for c in centroid]}
        item["Dim"] = {"N": str(len(centroid))}
    return item


class FakeTable:
    """A DynamoDB client that returns a fixed page, or raises."""

    def __init__(self, items, *, error=None, pages=None):
        self.items = items
        self.error = error
        self.pages = pages
        self.scans = 0

    def scan(self, **kwargs):  # noqa: ARG002
        self.scans += 1
        if self.error:
            raise self.error
        if self.pages is not None:
            # Always hands back a continuation key, to exercise the page bound.
            return {"Items": self.pages, "LastEvaluatedKey": {"SubjectId": {"S": str(self.scans)}}}
        return {"Items": self.items}


def run(items, **kwargs):
    seeds.dynamodb = FakeTable(items, **kwargs)
    return seeds.handler({"CallId": "call-1"}, None)


def test_sends_an_active_consented_voice():
    result = run([row("sub-1", name="Sarah Lee")])
    assert result["EnrolledVoices"] == [{"label": "Sarah Lee", "vector": [0.1, 0.2, 0.3]}]


def test_vector_components_are_plain_floats():
    """The engine's config is JSON, which has no Decimal."""
    [voice] = run([row("sub-1")])["EnrolledVoices"]
    assert all(isinstance(component, float) for component in voice["vector"])


def test_response_carries_no_subject_id():
    """A label and a vector are all the transcriber needs to recognise someone."""
    [voice] = run([row("sub-1")])["EnrolledVoices"]
    assert set(voice) == {"label", "vector"}


def test_withdrawn_consent_is_not_sent():
    assert run([row("sub-1", status="revoked", centroid=None)])["EnrolledVoices"] == []


def test_consent_to_superseded_terms_is_not_sent():
    assert run([row("sub-1", terms="0")])["EnrolledVoices"] == []


def test_lapsed_consent_is_not_sent():
    assert run([row("sub-1", expires_in_days=-1)])["EnrolledVoices"] == []


def test_pending_enrollment_with_no_voiceprint_is_not_sent():
    assert run([row("sub-1", status="pending", centroid=None)])["EnrolledVoices"] == []


def test_one_unusable_row_does_not_cost_the_others_their_names():
    result = run(
        [
            row("sub-1", name="Sarah Lee"),
            row("sub-2", name="Priya Raman", status="revoked", centroid=None),
            row("sub-3", name="Tom Okafor"),
        ]
    )
    assert sorted(v["label"] for v in result["EnrolledVoices"]) == ["Sarah Lee", "Tom Okafor"]


def test_two_people_with_the_same_name_are_both_sent():
    """Dropping one would silently discard somebody's enrollment."""
    result = run([row("sub-1", name="Sam"), row("sub-2", name="Sam", centroid=(0.4, 0.5, 0.6))])
    assert [v["label"] for v in result["EnrolledVoices"]] == ["Sam", "Sam"]
    assert len({tuple(v["vector"]) for v in result["EnrolledVoices"]}) == 2


def test_an_unreadable_table_yields_no_names_rather_than_a_failed_call():
    result = run([], error=RuntimeError("table is on fire"))
    assert result == {"EnrolledVoices": []}


def test_the_scan_is_bounded():
    """A table that keeps paging must not spin the call a meeting is waiting on."""
    seeds.dynamodb = FakeTable([], pages=[row("sub-1")])
    seeds.handler({"CallId": "call-1"}, None)
    assert seeds.dynamodb.scans == seeds.MAX_PAGES


def test_more_enrollments_than_fit_are_cut_to_the_bound():
    many = [row(f"sub-{i}", name=f"Person {i}") for i in range(seeds.MAX_SEEDS + 5)]
    assert len(run(many)["EnrolledVoices"]) == seeds.MAX_SEEDS


def test_the_newest_enrollments_survive_the_cut():
    """Which voices are dropped is decided here, where the dates are known."""
    old = [row(f"old-{i}", name=f"Old {i}", enrolled_at=iso(-400 + i)) for i in range(40)]
    recent = row("recent", name="Recent Person", enrolled_at=iso(-1))
    labels = [v["label"] for v in run([recent, *old])["EnrolledVoices"]]
    assert "Recent Person" in labels


def test_a_missing_call_id_still_answers():
    """The call id is only used for the log line, so its absence is not fatal."""
    seeds.dynamodb = FakeTable([row("sub-1")])
    assert len(seeds.handler({}, None)["EnrolledVoices"]) == 1


def test_session_seeds_reports_how_many_were_usable():
    """The caller needs the total to tell "none enrolled" from "too many"."""
    rows = [
        {
            "SubjectId": f"sub-{i}",
            "DisplayName": f"Person {i}",
            "Status": "active",
            "TermsVersion": "1",
            "EnrolledAt": iso(-i - 1),
            "ConsentExpiresAt": iso(30),
            "Centroid": [0.1, 0.2],
        }
        for i in range(5)
    ]
    voices, usable = pure.session_seeds(rows, "1", iso(0), 2)
    assert len(voices) == 2
    assert usable == 5


def test_session_seeds_is_stable_when_dates_tie():
    """Two enrollments stamped the same instant must not cut differently per call."""
    rows = [
        {
            "SubjectId": subject,
            "DisplayName": subject,
            "Status": "active",
            "TermsVersion": "1",
            "EnrolledAt": iso(-1),
            "ConsentExpiresAt": iso(30),
            "Centroid": [0.1, 0.2],
        }
        for subject in ("a", "b", "c")
    ]
    first, _ = pure.session_seeds(rows, "1", iso(0), 2)
    second, _ = pure.session_seeds(list(reversed(rows)), "1", iso(0), 2)
    assert [v["label"] for v in first] == [v["label"] for v in second]


def test_session_seeds_with_a_zero_limit_sends_nothing():
    rows = [
        {
            "SubjectId": "sub-1",
            "DisplayName": "Sarah Lee",
            "Status": "active",
            "TermsVersion": "1",
            "EnrolledAt": iso(-1),
            "ConsentExpiresAt": iso(30),
            "Centroid": [0.1, 0.2],
        }
    ]
    voices, usable = pure.session_seeds(rows, "1", iso(0), 0)
    assert voices == []
    assert usable == 1
