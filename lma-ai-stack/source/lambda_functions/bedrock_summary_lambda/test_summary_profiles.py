# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The summariser and summary profiles: which sections a meeting is summarised with,
and the structured action items a profile can ask for.

A meeting with no summary profile, or ``default``, must be summarised exactly as
before; the tests in test_index.py cover that path, and the ones here check
that nothing about it changes when the profiles exist.
"""

from __future__ import annotations

import io
import json

import pytest
from lma_config import codec  # noqa: E402
from lma_config.keys import DEFAULT_KEY  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402
from test_index import (
    CALL_ID,
    METADATA,
    MODEL_ID,
    TABLE_NAME,
    FakeBedrock,
    FakeS3Client,
    index,
    prompts_sent,
)

TIMED = "\n[00:05] Priya Shah: I will send the plan by 2026-12-01.\n[00:20] Sam Lee: agreed"
TRANSCRIPT = "Priya Shah: I will send the plan by 2026-12-01.\nSam Lee: agreed"
SEGMENTS = [
    {"segmentId": "seg-1", "startTime": 5.0, "endTime": 9.0, "speaker": "Priya Shah"},
    {"segmentId": "seg-2", "startTime": 20.0, "endTime": 22.0, "speaker": "Sam Lee"},
]
DEFAULT_PROMPTS = {
    "1#SUMMARY": "Default summary of {transcript}",
    "2#ACTIONS": "Actions {transcript}",
}
PROFILES = {
    "discovery-session": {
        "label": "Discovery session",
        "structuredActionItems": True,
        "prompts": {
            "1#SUMMARY": "Discovery summary of {transcript}",
            "2#DECISIONS": "Decisions in {timed_transcript}",
        },
    },
    "stand-up": {"label": "Stand-up", "prompts": {"1#SUMMARY": "Stand-up {transcript}"}},
    "empty": {"label": "Nothing in it", "prompts": {"1#SUMMARY": "NONE"}},
}


class Table:
    """The configuration table with both sections in its baseline."""

    def __init__(self, profiles=PROFILES):
        body = {"summary_prompt_templates": DEFAULT_PROMPTS}
        if profiles is not None:
            body["summary_profiles"] = profiles
        self.item = {
            "Configuration": {"S": DEFAULT_KEY},
            "Version": {"N": "1"},
            "Body": {"B": codec.encode_body(body)},
        }

    def get_item(self, **kwargs):
        return {"Item": self.item} if kwargs["Key"]["Configuration"]["S"] == DEFAULT_KEY else {}


class Fetch:
    """fetch_transcript: the transcript, and the segments when asked for them."""

    def __init__(self, metadata=None):
        self.requests = []
        self.metadata = dict(METADATA, **(metadata or {}))

    def invoke(self, **kwargs):
        payload = json.loads(kwargs["Payload"])
        self.requests.append(payload)
        body = {"transcript": TRANSCRIPT, "metadata": self.metadata, "timedTranscript": TIMED}
        if payload.get("IncludeSegments"):
            body["segments"] = SEGMENTS
        return {"Payload": io.BytesIO(json.dumps(body).encode())}


def install(monkeypatch, texts=(), metadata=None, profiles=PROFILES):
    bedrock = FakeBedrock(texts=list(texts))
    fetch = Fetch(metadata)
    monkeypatch.setattr(index, "bedrock", bedrock)
    monkeypatch.setattr(index, "summary_model_id", lambda: MODEL_ID)
    monkeypatch.setattr(
        index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=Table(profiles))
    )
    monkeypatch.setattr(index, "lambda_client", fetch)
    monkeypatch.setattr(index.boto3, "client", lambda *_a, **_kw: FakeS3Client())
    return bedrock, fetch


ACTION_ANSWER = json.dumps(
    [
        {
            "text": "Send the plan",
            "owner": "Priya Shah",
            "dueDate": "2026-12-01",
            "dueText": "by the first of December",
            "startTime": 6,
        },
        {"text": "Book a follow-up", "owner": "the account team", "startTime": 21},
    ]
)


# ── the default profile ─────────────────────────────────────────────────────


@pytest.mark.parametrize("profile", [None, "", "default"])
def test_a_meeting_with_no_profile_is_summarised_as_before(monkeypatch, profile) -> None:
    """The same requests, the same answer, and only the summary key in it."""
    metadata = {} if profile is None else {"SummaryProfile": profile}
    bedrock, fetch = install(monkeypatch, metadata=metadata)
    result = index.handler({"CallId": CALL_ID}, None)
    assert list(result) == ["summary"]
    assert prompts_sent(bedrock) == [
        f"Default summary of {TRANSCRIPT}",
        f"Actions {TRANSCRIPT}",
    ]
    # No second read of the transcript, and no action-item call.
    assert len(fetch.requests) == 1
    assert "IncludeSegments" not in fetch.requests[0]


def test_the_default_path_does_not_read_the_profiles(monkeypatch) -> None:
    """Even a configuration with no summary_profiles section at all summarises as before."""
    bedrock, _ = install(monkeypatch, profiles=None)
    result = index.handler({"CallId": CALL_ID}, None)
    assert list(result) == ["summary"]
    assert len(bedrock.requests) == 2


# ── choosing a profile ──────────────────────────────────────────────────────


def test_a_meetings_profile_chooses_its_sections(monkeypatch) -> None:
    bedrock, _ = install(monkeypatch, texts=["s", "d"], metadata={"SummaryProfile": "stand-up"})
    result = index.handler({"CallId": CALL_ID}, None)
    assert prompts_sent(bedrock) == [f"Stand-up {TRANSCRIPT}"]
    assert result == {"summary": "s", "summaryProfile": "stand-up"}


def test_a_regeneration_can_choose_another_profile(monkeypatch) -> None:
    bedrock, _ = install(monkeypatch, metadata={"SummaryProfile": "discovery-session"})
    result = index.handler(
        {"CallId": CALL_ID, "RefreshConfiguration": True, "SummaryProfile": "stand-up"}, None
    )
    assert prompts_sent(bedrock) == [f"Stand-up {TRANSCRIPT}"]
    assert result["summaryProfile"] == "stand-up"
    assert "actionItems" not in result


def test_a_regeneration_can_choose_the_default_for_a_meeting_with_a_profile(monkeypatch) -> None:
    bedrock, _ = install(monkeypatch, metadata={"SummaryProfile": "stand-up"})
    result = index.handler({"CallId": CALL_ID, "SummaryProfile": "default"}, None)
    assert prompts_sent(bedrock)[0] == f"Default summary of {TRANSCRIPT}"
    assert "summaryProfile" not in result


@pytest.mark.parametrize("profile", ["retired-profile", "empty", "Not A Name"])
def test_a_profile_that_cannot_be_used_falls_back_to_the_default(monkeypatch, profile) -> None:
    bedrock, _ = install(monkeypatch, metadata={"SummaryProfile": profile})
    result = index.handler({"CallId": CALL_ID}, None)
    assert prompts_sent(bedrock)[0] == f"Default summary of {TRANSCRIPT}"
    assert result["summaryProfile"] == "default"
    assert result["summaryProfileFallback"] == profile


def test_a_callers_own_prompt_has_no_profile(monkeypatch) -> None:
    bedrock, _ = install(monkeypatch, metadata={"SummaryProfile": "discovery-session"})
    result = index.handler({"CallId": CALL_ID, "Prompt": "Q {transcript}"}, None)
    assert prompts_sent(bedrock) == [f"Q {TRANSCRIPT}"]
    assert list(result) == ["summary"]


# ── structured action items ─────────────────────────────────────────────────


def test_a_structured_profile_lists_its_action_items(monkeypatch) -> None:
    bedrock, fetch = install(
        monkeypatch,
        texts=["summary", "decisions", ACTION_ANSWER],
        metadata={"SummaryProfile": "discovery-session"},
    )
    result = index.handler({"CallId": CALL_ID}, None)
    # The two sections, then the one extra call, over the timed transcript.
    assert len(bedrock.requests) == 3
    assert TIMED.strip() in prompts_sent(bedrock)[2]
    assert fetch.requests[1]["IncludeSegments"] is True
    assert result["summaryProfile"] == "discovery-session"
    assert result["actionItemsStatus"] == "generated"
    assert result["actionItems"] == [
        {
            "Text": "Send the plan",
            "Owner": "Priya Shah",
            "DueDate": "2026-12-01",
            "DueText": "by the first of December",
            "StartTime": 6.0,
            "SegmentId": "seg-1",
        },
        {
            # Nobody of that name spoke or was named: no owner, not a guess.
            "Text": "Book a follow-up",
            "Owner": None,
            "DueDate": None,
            "DueText": None,
            "StartTime": 21.0,
            "SegmentId": "seg-2",
        },
    ]
    assert json.loads(result["summary"]) == {"SUMMARY": "summary", "DECISIONS": "decisions"}


def test_a_malformed_action_item_answer_keeps_the_summary(monkeypatch) -> None:
    _, _ = install(
        monkeypatch,
        texts=["summary", "decisions", "Here are the action items: send the plan."],
        metadata={"SummaryProfile": "discovery-session"},
    )
    result = index.handler({"CallId": CALL_ID}, None)
    assert json.loads(result["summary"])["SUMMARY"] == "summary"
    assert result["actionItemsStatus"] == "failed"
    # An earlier list is not kept under this summary.
    assert result["actionItems"] == []


def test_a_failed_action_item_call_keeps_the_summary(monkeypatch) -> None:
    bedrock, _ = install(
        monkeypatch,
        texts=["summary", "decisions"],
        metadata={"SummaryProfile": "discovery-session"},
    )
    calls = []
    original = bedrock.converse

    def converse(**kwargs):
        calls.append(kwargs)
        if len(calls) == 3:
            raise RuntimeError("throttled")
        return original(**kwargs)

    bedrock.converse = converse
    result = index.handler({"CallId": CALL_ID}, None)
    assert json.loads(result["summary"])["DECISIONS"] == "decisions"
    assert result["actionItemsStatus"] == "failed"


@pytest.mark.parametrize("profile", [None, "stand-up"])
def test_a_summary_without_action_items_clears_the_ones_an_earlier_summary_left(
    monkeypatch, profile
) -> None:
    earlier = [{"Text": "Send the plan", "Owner": "Priya Shah"}]
    metadata = {"ActionItems": earlier, **({"SummaryProfile": profile} if profile else {})}
    bedrock, _ = install(monkeypatch, metadata=metadata)
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["actionItems"] == []
    # No extra model call: clearing needs none.
    assert len(bedrock.requests) == (2 if profile is None else 1)


def test_a_profile_without_structured_items_makes_no_extra_call(monkeypatch) -> None:
    bedrock, fetch = install(monkeypatch, metadata={"SummaryProfile": "stand-up"})
    index.handler({"CallId": CALL_ID}, None)
    assert len(bedrock.requests) == 1
    assert len(fetch.requests) == 1
