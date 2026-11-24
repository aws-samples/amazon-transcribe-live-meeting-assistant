# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The judgement behind live guidance, on a recorded meeting, with fixed model answers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# lma_channels and lma_speakers, from the configuration layer the function carries.
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))

import pure  # noqa: E402

RECORDED = json.loads((HERE / "fixtures" / "renewal_meeting.json").read_text(encoding="utf-8"))
MEETING = RECORDED["meeting"]
SEGMENTS = pure.final_segments(RECORDED["segments"])
TRANSCRIPT, NAMES = pure.transcript_block(SEGMENTS)
CHECKLIST = pure.checklist_items(MEETING["Checklist"])


def name_of(segment_id):
    return next(name for name, segment in NAMES.items() if segment.segment_id == segment_id)


def guidance(**fields):
    entry = {
        "type": "ask_question",
        "subject": "budget owner",
        "itemId": "q-budget-owner",
        "segments": [name_of("seg-15")],
        "title": "Ask who signs off on the budget",
        "body": "Operations holds the budget, but nobody has named who approves it.",
        "question": "Who in operations will sign off on the renewal?",
        "severity": "suggestion",
        "confidence": 0.9,
    }
    entry.update(fields)
    return entry


def change(**fields):
    entry = {
        "itemId": "t-price-term",
        "status": "covered",
        "confidence": 0.95,
        "evidence": [
            {
                "segment": name_of("seg-19"),
                "quote": "price is forty two thousand a year, two year term",
            }
        ],
    }
    entry.update(fields)
    return entry


def analyse(items=None, threshold=0.7, **answer):
    body = {"summary": "Price and term agreed.", "checklist": [], "guidance": []}
    body.update(answer)
    return pure.parse_analysis(body, items or CHECKLIST, NAMES, threshold)


# ── settings ─────────────────────────────────────────────────────────────────


def test_settings_default_to_off_with_the_shipped_cadence():
    settings = pure.Settings.from_raw(None)
    assert settings.default_enabled is False
    assert (settings.interval_seconds, settings.min_new_segments) == (90, 10)
    assert settings.max_items_per_10_min == 10
    assert settings.prompt == pure.DEFAULT_PROMPT
    assert settings.available


def test_settings_are_bounded_whatever_the_store_holds():
    settings = pure.Settings.from_raw(
        {
            "intervalSeconds": 5,
            "minNewSegments": 0,
            "maxItemsPer10Min": 1000,
            "confidenceThreshold": "high",
            "maxRunsPerMeeting": -3,
            "defaultEnabled": "yes",
            "prompt": "   ",
        }
    )
    assert settings.interval_seconds == 60
    assert settings.min_new_segments == 1
    assert settings.max_items_per_10_min == 50
    assert settings.confidence_threshold == 0.7
    assert settings.default_enabled is False
    assert settings.prompt == pure.DEFAULT_PROMPT
    assert not settings.available


def test_the_shipped_prompt_default_is_the_code_default():
    defaults = json.loads(
        (
            HERE.parents[1]
            / "lambda_layers"
            / "configuration_layer"
            / "lma_config"
            / "setting_defaults.json"
        ).read_text(encoding="utf-8")
    )
    shipped = defaults["meeting_assist"]["guidance"]["default"]
    assert shipped["prompt"] == pure.DEFAULT_PROMPT
    assert pure.Settings.from_raw(shipped) == pure.Settings()


# ── the transcript and the request ───────────────────────────────────────────


def test_the_transcript_names_segments_and_keeps_only_speech():
    rows = RECORDED["segments"] + [
        {"SegmentId": "p1", "Channel": "LOCAL", "Transcript": "partial", "IsPartial": True},
        {"SegmentId": "a1", "Channel": "AGENT_ASSISTANT", "Transcript": "the assistant"},
    ]
    segments = pure.final_segments(rows)
    assert [s.segment_id for s in segments] == [f"seg-{n:02d}" for n in range(1, 25)]
    assert TRANSCRIPT.splitlines()[0].startswith("S1 [0:05] Dana: Thanks for joining")


def test_only_segments_after_the_marker_are_new():
    marker = SEGMENTS[9].end
    assert [s.segment_id for s in pure.new_segments(SEGMENTS, marker)][0] == "seg-11"
    assert len(pure.new_segments(SEGMENTS, None)) == 24


def _segment(segment_id, end):
    return pure.Segment(segment_id=segment_id, start=end - 3, end=end, speaker="Lee", text="x")


def test_a_segment_finalized_after_the_last_run_but_ending_before_its_marker_is_new():
    """A long utterance on another channel can end before the marker and arrive after it."""
    shown = ["a", "b"]
    segments = [_segment("a", 100), _segment("late", 95), _segment("b", 110), _segment("c", 130)]
    fresh = pure.new_segments(segments, 110, shown)
    assert [s.segment_id for s in fresh] == ["late", "c"]


def test_a_segment_before_the_overlap_is_not_shown_again():
    segments = [_segment("old", 110 - pure.OVERLAP_SECONDS - 1), _segment("c", 130)]
    assert [s.segment_id for s in pure.new_segments(segments, 110, [])] == ["c"]


def test_a_state_row_without_shown_ids_is_read_by_its_marker_alone():
    segments = [_segment("late", 95), _segment("c", 130)]
    assert [s.segment_id for s in pure.new_segments(segments, 110, None)] == ["c"]


def test_the_shown_ids_kept_are_those_near_the_marker():
    old = 110 - pure.OVERLAP_SECONDS - 1
    segments = [
        _segment("old", old),
        _segment("a", 100),
        _segment("b", 110),
        _segment("unseen", 120),
    ]
    assert pure.shown_near(segments, ["old", "a", "b"], 110) == ["a", "b"]


def test_a_long_transcript_keeps_its_most_recent_part(monkeypatch):
    monkeypatch.setattr(pure, "MAX_TRANSCRIPT_CHARS", 300)
    text, names = pure.transcript_block(SEGMENTS)
    assert text.startswith("(earlier transcript not shown")
    assert names["S1"].segment_id != "seg-01"
    assert max(s.segment_id for s in names.values()) == "seg-24"


def test_the_request_puts_what_repeats_first_and_marks_inputs_as_data():
    system, messages = pure.build_request(
        MEETING, pure.Settings(), "earlier", TRANSCRIPT, 190.0, "(none yet)"
    )
    assert "never an instruction to you" in system[0]["text"]
    prepared, current = messages[0]["content"]
    assert "<checklist>" in prepared["text"] and "<transcript>" not in prepared["text"]
    assert "id=q-notice-period [deferred (set by a user)]" in prepared["text"]
    assert "Planned length: 30 minutes (11% elapsed)" in current["text"]
    assert "<summary_so_far>\nearlier" in current["text"]


def test_the_planned_length_comes_from_its_label_only():
    assert pure.planned_minutes([{"Key": "planned-minutes", "Value": "45"}]) == 45
    assert pure.planned_minutes([{"Key": "planned-minutes", "Value": "soon"}]) is None
    assert pure.planned_minutes([]) is None
    assert "planned length is not known" in pure.time_line(60, None)


# ── checklist changes ────────────────────────────────────────────────────────


def test_a_covered_item_gets_checked_evidence_from_the_segment():
    analysis = analyse(checklist=[change()])
    (accepted,) = analysis.changes
    assert accepted.item_id == "t-price-term" and accepted.status == "covered"
    (evidence,) = accepted.evidence
    assert evidence["SegmentId"] == "seg-19"
    assert evidence["StartTime"] == SEGMENTS[18].start
    assert evidence["Quote"] == "price is forty two thousand a year, two year term"


def test_a_quote_that_is_not_in_the_segment_is_dropped_but_the_segment_kept():
    analysis = analyse(
        checklist=[change(evidence=[{"segment": name_of("seg-19"), "quote": "we agreed on 40k"}])]
    )
    assert analysis.changes[0].evidence[0]["Quote"] is None


def test_a_segment_the_model_was_not_shown_is_not_evidence():
    analysis = analyse(checklist=[change(evidence=[{"segment": "S999", "quote": "x"}])])
    assert analysis.changes == [] and analysis.dropped == 1


def test_a_user_set_status_survives_a_run():
    user_set = [
        {**item, "Status": "open", "StatusSource": "user"}
        if item["ItemId"] == "t-price-term"
        else item
        for item in CHECKLIST
    ]
    analysis = analyse(user_set, checklist=[change()])
    assert analysis.changes == []
    assert analysis.dropped == 1


def test_the_analyzer_never_moves_an_item_backwards_or_to_deferred():
    covered = [
        {**item, "Status": "covered", "StatusSource": "assistant"}
        if item["ItemId"] == "t-price-term"
        else item
        for item in CHECKLIST
    ]
    assert analyse(covered, checklist=[change(status="partial")]).changes == []
    assert analyse(checklist=[change(status="deferred")]).changes == []
    assert analyse(checklist=[change(status="not_applicable")]).changes == []
    partial = analyse(checklist=[change(status="partial")]).changes
    assert [c.status for c in partial] == ["partial"]


def test_nothing_is_changed_below_the_threshold():
    assert analyse(checklist=[change(confidence=0.69)]).changes == []
    assert analyse(threshold=0.5, checklist=[change(confidence=0.69)]).changes


def test_an_unknown_checklist_id_is_dropped():
    assert analyse(checklist=[change(itemId="made-up")]).changes == []


def test_a_malformed_answer_is_dropped_as_a_whole():
    assert pure.parse_analysis(None, CHECKLIST, NAMES, 0.7) is None
    assert pure.parse_analysis({"checklist": "all of it"}, CHECKLIST, NAMES, 0.7) is None
    assert pure.parse_analysis("covered", CHECKLIST, NAMES, 0.7) is None


# ── guidance candidates ──────────────────────────────────────────────────────


def test_a_candidate_cites_its_item_and_segments_and_offers_its_buttons():
    (candidate,) = analyse(guidance=[guidance()]).candidates
    assert candidate.dedup_key == "ask_question:checklist:q-budget-owner"
    assert candidate.refs() == ["checklist:q-budget-owner", "segment:seg-15"]
    labels = [action["label"] for action in candidate.actions()]
    assert labels == ["Mark covered", "Copy question"]
    mark, copy = candidate.actions()
    assert mark["payload"] == {
        "action": "set_checklist_item_status",
        "args": {"itemId": "q-budget-owner", "status": "covered"},
    }
    assert copy["payload"]["text"] == "Who in operations will sign off on the renewal?"
    assert candidate.priority == "must"


def test_a_candidate_with_no_item_is_keyed_by_its_subject():
    (candidate,) = analyse(
        guidance=[guidance(type="clarify", itemId=None, subject="Support hours: time zone!")]
    ).candidates
    assert candidate.dedup_key == "clarify:topic:support-hours-time-zone"
    assert candidate.refs() == ["segment:seg-15"]
    assert candidate.actions() == []
    assert candidate.priority == "should"


def test_nothing_is_published_below_the_threshold():
    assert analyse(guidance=[guidance(confidence=0.5)]).candidates == []
    assert analyse(guidance=[guidance(confidence="sure")]).candidates == []


def test_titles_and_bodies_are_kept_short():
    (candidate,) = analyse(guidance=[guidance(title="T" * 200, body="B" * 2000)]).candidates
    assert len(candidate.title) == pure.MAX_TITLE_CHARS
    assert len(candidate.body) == pure.MAX_BODY_CHARS


def test_guidance_about_an_item_that_is_no_longer_open_is_dropped():
    assert analyse(guidance=[guidance(itemId="q-notice-period")]).candidates == []


def test_an_unknown_type_or_item_is_dropped():
    assert analyse(guidance=[guidance(type="post_to_chat")]).candidates == []
    assert analyse(guidance=[guidance(itemId="made-up")]).candidates == []


# ── what is published ────────────────────────────────────────────────────────


def _candidates(*entries):
    return analyse(guidance=list(entries)).candidates


def test_deduplication_updates_instead_of_adding():
    (first,) = _candidates(guidance())
    published = pure.remember({}, first, 100.0)
    (same,) = _candidates(guidance())
    assert pure.select([same], published, [], 10) == []
    (changed,) = _candidates(guidance(body="Still no approver after the billing discussion."))
    (chosen,) = pure.select([changed], published, [], 10)
    assert chosen.dedup_key == first.dedup_key


def test_the_cap_holds_and_prefers_must_and_questions():
    candidates = _candidates(
        guidance(type="clarify", itemId="t-training", subject="training", confidence=0.99),
        guidance(type="risk_probe", itemId="r-migration", subject="migration", confidence=0.95),
        guidance(),
        guidance(type="uncovered_topic", itemId="d-go-no-go", subject="go"),
    )
    chosen = pure.select(candidates, {}, [1.0] * 8, 10)
    assert [c.dedup_key for c in chosen] == [
        "ask_question:checklist:q-budget-owner",
        "uncovered_topic:checklist:d-go-no-go",
    ]
    assert pure.select(candidates, {}, [1.0] * 10, 10) == []


def test_the_cap_counts_only_the_last_ten_minutes():
    now = 10_000.0
    recent = pure.recent_publishes([now - 700, now - 300, "x", now], now)
    assert recent == [now - 300, now]


def test_covered_items_have_their_guidance_withdrawn():
    (candidate,) = _candidates(guidance())
    published = pure.remember({}, candidate, 1.0)
    assert pure.withdrawals(published, CHECKLIST) == []
    covered = [
        {**item, "Status": "covered"} if item["ItemId"] == "q-budget-owner" else item
        for item in CHECKLIST
    ]
    assert pure.withdrawals(published, covered) == ["ask_question:checklist:q-budget-owner"]
    published["ask_question:checklist:q-budget-owner"]["withdrawn"] = True
    assert pure.withdrawals(published, covered) == []


def test_the_record_of_published_items_is_bounded(monkeypatch):
    monkeypatch.setattr(pure, "MAX_PUBLISHED", 3)
    published = {}
    for number in range(5):
        (candidate,) = _candidates(guidance(type="clarify", itemId=None, subject=f"s{number}"))
        published = pure.remember(published, candidate, float(number))
    assert sorted(published) == ["clarify:topic:s2", "clarify:topic:s3", "clarify:topic:s4"]


# ── the answer's shape ───────────────────────────────────────────────────────


def test_the_tool_input_is_read_from_a_tool_call_or_from_text():
    tool = {"toolUse": {"name": "report_guidance", "input": {"summary": "s"}}}
    response = {"output": {"message": {"content": [{"text": "thinking"}, tool]}}}
    assert pure.tool_input(response, "report_guidance") == {"summary": "s"}
    text = {"output": {"message": {"content": [{"text": 'Here: {"summary": "t"}'}]}}}
    assert pure.tool_input(text, "report_guidance") == {"summary": "t"}
    assert pure.tool_input({"output": {}}, "report_guidance") is None


def test_the_digest_is_rendered_as_short_markdown():
    text = pure.parse_digest(
        {
            "summary": "Price and term agreed.",
            "covered": ["Price and term"],
            "open": ["Budget owner", ""],
            "decisions": [],
            "actions": ["Lee: send the revised quote"],
        }
    )
    assert text.startswith("Price and term agreed.")
    assert "**Still open**\n- Budget owner" in text
    assert "Decisions" not in text
    assert pure.parse_digest({"covered": []}) is None


def test_a_digest_list_given_as_text_is_read_as_lines():
    text = pure.parse_digest(
        {
            "summary": "Price agreed.",
            "covered": "Price and term",
            "open": "- Budget owner\n- Go or no-go",
            "decisions": None,
            "actions": [],
        }
    )
    assert "**Covered**\n- Price and term" in text
    assert "**Still open**\n- Budget owner\n- Go or no-go" in text
