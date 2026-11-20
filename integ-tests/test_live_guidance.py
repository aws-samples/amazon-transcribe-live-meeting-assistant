# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Live guidance on a live stack.

One probe run (live_guidance_probe.py) creates a meeting with live guidance on
and a control meeting with it off, each with the recorded 8-item checklist and
transcript, and observes what the analyzer did. Each test asserts one property.
The model's judgement varies from run to run, so the assertions about it are
loose: an item marked covered or partial with evidence from the meeting, and a
guidance item that cites a checklist item. The guidance the run produced is
printed, so a reader can judge its quality.

Runs whenever a Cognito test user is configured, and skips without one. Takes
about five minutes: the transcript is sent in two waves an interval apart.
Self-cleaning: both meetings are removed by their CallIds.
"""

from __future__ import annotations

import json

import cognito_test_user
import pytest

USER_SET_ITEM = "q-notice-period"


@pytest.fixture(scope="module")
def observed(client):
    if not cognito_test_user.available(client.region):
        pytest.skip(f"no Cognito test user — {cognito_test_user.describe_sources()}")
    try:
        import pycognito  # noqa: F401
    except ImportError:
        pytest.skip("pycognito not installed (pip install -r integ-tests/requirements.txt)")

    from live_guidance_probe import run_probe

    result = run_probe(client.stack_name, region=client.region)
    print(json.dumps(result, indent=1, default=str))
    return result


def test_the_meetings_were_created_with_their_guidance_choice(observed) -> None:
    assert observed["guided"]["guidance_enabled"] is True
    assert observed["control"]["guidance_enabled"] is False


def test_with_guidance_off_nothing_runs_and_nothing_is_billed(observed) -> None:
    control = observed["control"]
    assert control["has_state"] is False, control
    assert "LiveGuidance" not in control["metering_contexts"], control["metering_contexts"]
    assert control["guidance"] == []
    assert all(item["StatusSource"] != "assistant" for item in control["checklist"])


def test_a_meeting_with_guidance_on_is_analysed_and_billed(observed) -> None:
    guided = observed["guided"]
    assert guided["run_count"] >= 1, guided
    assert guided["has_summary"], "the run kept no summary of the meeting so far"
    assert "LiveGuidance" in guided["metering_contexts"], guided["metering_contexts"]


def test_an_item_the_transcript_covers_is_marked_by_the_assistant_with_evidence(observed) -> None:
    changed = [
        item
        for item in observed["guided"]["checklist"]
        if item["StatusSource"] == "assistant" and item["Status"] in ("covered", "partial")
    ]
    assert changed, observed["guided"]["checklist"]
    call_id = observed["guided_call_id"]
    for item in changed:
        assert item["Evidence"], item
        assert all(str(e.get("SegmentId", "")).startswith(call_id) for e in item["Evidence"]), item


def test_a_status_a_user_set_survives(observed) -> None:
    (item,) = [i for i in observed["guided"]["checklist"] if i["ItemId"] == USER_SET_ITEM]
    assert item["Status"] == "partial" and item["StatusSource"] == "user", item


def test_guidance_is_published_and_cites_a_checklist_item(observed) -> None:
    guidance = [g for g in observed["guided"]["guidance"] if g["Source"] == "live-guidance"]
    assert guidance, "no guidance was published"
    assert any(
        any(ref.startswith("checklist:") for ref in g["Refs"]) for g in guidance
    ), guidance
    for item in guidance:
        assert item["DedupKey"].startswith(f"{item['Type']}:"), item
        assert len(item["Title"] or "") <= 80 and len(item["Body"] or "") <= 1500, item


def test_a_digest_is_published_for_the_owner(observed) -> None:
    digest = observed["digest"]
    assert digest["Success"] is True, digest
    assert digest["Text"], digest
    types = [g["Type"] for g in observed["guided_after_digest"]["guidance"]]
    assert "meeting_digest" in types, types
