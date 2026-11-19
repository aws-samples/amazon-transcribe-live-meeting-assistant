#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the meeting preparation rules: every limit, the replace merge, the
status precedence, the evidence cap, quote removal and the ``{checklist}`` text."""

from __future__ import annotations

import pytest

from lma_meeting_preparation import rules
from lma_meeting_preparation.rules import PreparationError

NOW = "2026-11-19T10:00:00.000Z"
LATER = "2026-11-19T11:00:00.000Z"


def definition(item_id="q1", text="Who owns the budget?", kind="question", **extra):
    return {"ItemId": item_id, "Text": text, "Kind": kind, **extra}


def checklist(*definitions):
    items = rules.normalize_definitions(list(definitions))
    return rules.merge_checklist([], items, reset_statuses=False, updated_by="owner", now=NOW)


# ── labels ───────────────────────────────────────────────────────────────────


def test_labels_are_kept_in_order():
    labels = [{"Key": "project", "Value": "website-relaunch"}, {"Key": "team.a_b-1", "Value": ""}]
    assert rules.normalize_labels(labels) == labels


def test_no_labels_is_an_empty_list():
    assert rules.normalize_labels(None) == []
    assert rules.normalize_labels([]) == []


def test_at_most_twenty_labels():
    labels = [{"Key": f"k{i}", "Value": "v"} for i in range(20)]
    assert len(rules.normalize_labels(labels)) == 20
    with pytest.raises(PreparationError, match="at most 20 labels"):
        rules.normalize_labels(labels + [{"Key": "k20", "Value": "v"}])


@pytest.mark.parametrize("key", ["", "Project", "-project", ".a", "a b", "a" * 64, "ä"])
def test_a_label_key_outside_the_pattern_is_refused(key):
    with pytest.raises(PreparationError, match="label Key"):
        rules.normalize_labels([{"Key": key, "Value": "v"}])


def test_a_label_key_of_63_characters_is_accepted():
    assert rules.normalize_labels([{"Key": "a" * 63, "Value": "v"}])[0]["Key"] == "a" * 63


def test_a_label_value_is_at_most_256_characters():
    assert rules.normalize_labels([{"Key": "k", "Value": "v" * 256}])
    with pytest.raises(PreparationError, match="longer than 256"):
        rules.normalize_labels([{"Key": "k", "Value": "v" * 257}])


def test_a_repeated_label_key_is_refused():
    with pytest.raises(PreparationError, match="more than once"):
        rules.normalize_labels([{"Key": "k", "Value": "1"}, {"Key": "k", "Value": "2"}])


def test_a_label_needs_a_string_value():
    with pytest.raises(PreparationError, match="must have a Value"):
        rules.normalize_labels([{"Key": "k"}])


# ── audience and context ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected", [("internal", "internal"), ("external", "external"), (None, None), ("", None)]
)
def test_audience_values(value, expected):
    assert rules.normalize_audience(value) == expected


@pytest.mark.parametrize("value", ["Internal", "public", "both"])
def test_any_other_audience_is_refused(value):
    with pytest.raises(PreparationError, match="Audience must be one of internal, external"):
        rules.normalize_audience(value)


def test_the_context_is_at_most_20000_characters():
    assert rules.normalize_context("x" * 20000) == "x" * 20000
    with pytest.raises(PreparationError, match="longer than 20000"):
        rules.normalize_context("x" * 20001)


def test_an_empty_context_clears_it():
    assert rules.normalize_context(None) == ""
    assert rules.normalize_context("  \n") == ""


# ── checklist definitions ────────────────────────────────────────────────────


def test_an_item_defaults_to_should_and_open():
    (item,) = checklist(definition())
    assert item["Priority"] == "should"
    assert item["Status"] == "open"
    assert item["StatusSource"] is None
    assert item["Evidence"] == []
    assert item["UpdatedBy"] == "owner" and item["UpdatedAt"] == NOW


def test_at_most_100_items():
    items = [definition(f"i{i}") for i in range(100)]
    assert len(rules.normalize_definitions(items)) == 100
    with pytest.raises(PreparationError, match="at most 100 items"):
        rules.normalize_definitions(items + [definition("i100")])


@pytest.mark.parametrize("item_id", ["", "a b", "a/b", "x" * 65, None, 5])
def test_an_item_id_outside_the_pattern_is_refused(item_id):
    with pytest.raises(PreparationError, match="ItemId is 1 to 64"):
        rules.normalize_definitions([definition(item_id)])


def test_an_item_id_may_use_every_allowed_character():
    item_id = "Ab9._:-" + "x" * 57
    assert rules.normalize_definitions([definition(item_id)])[0]["ItemId"] == item_id


def test_item_ids_are_unique():
    with pytest.raises(PreparationError, match="more than one item"):
        rules.normalize_definitions([definition("a"), definition("a")])


def test_item_text_is_required_and_at_most_500_characters():
    assert rules.normalize_definitions([definition(text="t" * 500)])
    with pytest.raises(PreparationError, match="longer than 500"):
        rules.normalize_definitions([definition(text="t" * 501)])
    with pytest.raises(PreparationError, match="Text is required"):
        rules.normalize_definitions([definition(text="  ")])


@pytest.mark.parametrize("kind", rules.KINDS)
def test_every_kind_is_accepted(kind):
    assert rules.normalize_definitions([definition(kind=kind)])[0]["Kind"] == kind


def test_another_kind_is_refused():
    with pytest.raises(PreparationError, match="Kind must be one of"):
        rules.normalize_definitions([definition(kind="idea")])


def test_another_priority_is_refused():
    with pytest.raises(PreparationError, match="Priority must be one of must, should, could"):
        rules.normalize_definitions([definition(Priority="high")])


def test_the_source_is_returned_unchanged_up_to_256_characters():
    source = '  ref:{"a": 1}  '
    assert rules.normalize_definitions([definition(Source=source)])[0]["Source"] == source
    with pytest.raises(PreparationError, match="Source is longer than 256"):
        rules.normalize_definitions([definition(Source="s" * 257)])


# ── replace ──────────────────────────────────────────────────────────────────


def progressed():
    items = checklist(definition("a"), definition("b", "Confirm the date", "confirm"))
    items, _ = rules.apply_item_update(
        items,
        {
            "ItemId": "a",
            "Status": "covered",
            "Note": "Agreed",
            "Evidence": [{"SegmentId": "s1", "StartTime": 64.5, "Quote": "Dana owns it"}],
        },
        status_source="user",
        updated_by="owner",
        now=NOW,
        quotes_allowed=True,
    )
    return items


def test_a_replace_keeps_the_progress_of_an_item_already_present():
    items = progressed()
    replaced = rules.merge_checklist(
        items,
        rules.normalize_definitions(
            [definition("c", "New topic", "topic"), definition("a", "Who owns the budget now?")]
        ),
        reset_statuses=False,
        updated_by="shared",
        now=LATER,
    )
    assert [item["ItemId"] for item in replaced] == ["c", "a"]
    a = replaced[1]
    assert a["Text"] == "Who owns the budget now?"
    assert a["Status"] == "covered" and a["StatusSource"] == "user"
    assert a["Note"] == "Agreed"
    assert a["Evidence"] == [{"SegmentId": "s1", "StartTime": 64.5, "Quote": "Dana owns it"}]
    # Its definition changed, so the change is attributed to whoever replaced it.
    assert a["UpdatedBy"] == "shared" and a["UpdatedAt"] == LATER
    assert replaced[0]["Status"] == "open"


def test_an_unchanged_item_keeps_who_changed_it_last():
    items = progressed()
    replaced = rules.merge_checklist(
        items,
        rules.normalize_definitions([definition("a")]),
        reset_statuses=False,
        updated_by="shared",
        now=LATER,
    )
    assert replaced[0]["UpdatedBy"] == "owner" and replaced[0]["UpdatedAt"] == NOW


def test_an_item_left_out_of_a_replace_is_removed():
    replaced = rules.merge_checklist(
        progressed(),
        rules.normalize_definitions([definition("b", "Confirm", "confirm")]),
        reset_statuses=False,
        updated_by="owner",
        now=LATER,
    )
    assert [item["ItemId"] for item in replaced] == ["b"]


def test_a_reset_starts_every_item_again_as_open():
    replaced = rules.merge_checklist(
        progressed(),
        rules.normalize_definitions([definition("a")]),
        reset_statuses=True,
        updated_by="owner",
        now=LATER,
    )
    (a,) = replaced
    assert a["Status"] == "open"
    assert a["StatusSource"] is None and a["Note"] is None and a["Evidence"] == []
    assert a["UpdatedAt"] == LATER


# ── item updates and precedence ──────────────────────────────────────────────


def update(items, source="assistant", **change):
    return rules.apply_item_update(
        items,
        {"ItemId": "a", **change},
        status_source=source,
        updated_by="who",
        now=LATER,
        quotes_allowed=True,
    )


def test_the_assistant_never_changes_a_status_a_user_set():
    with pytest.raises(rules.StatusSetByUser, match="set by a user"):
        update(progressed(), Status="partial")


def test_the_assistant_cannot_add_to_an_item_a_user_set_either():
    with pytest.raises(rules.StatusSetByUser):
        update(progressed(), AddEvidence=[{"StartTime": 1}])


def test_a_user_changes_a_status_a_user_set():
    items, item = update(progressed(), source="user", Status="partial")
    assert item["Status"] == "partial" and item["StatusSource"] == "user"
    assert item["UpdatedBy"] == "who"


def test_the_assistant_changes_a_status_the_assistant_set_and_a_user_overrides_it():
    items = checklist(definition("a"))
    items, item = update(items, Status="partial")
    assert item["StatusSource"] == "assistant"
    items, item = update(items, Status="covered")
    assert item["Status"] == "covered"
    items, item = update(items, source="user", Status="deferred")
    assert item["StatusSource"] == "user"
    with pytest.raises(rules.StatusSetByUser):
        update(items, Status="covered")


def test_a_note_alone_leaves_the_status_and_its_source():
    items, item = update(progressed(), source="user", Note="Follow up")
    assert (
        item["Status"] == "covered"
        and item["StatusSource"] == "user"
        and item["Note"] == "Follow up"
    )


def test_an_empty_note_clears_it():
    _, item = update(progressed(), source="user", Note="")
    assert item["Note"] is None


def test_a_note_is_at_most_1000_characters():
    update(checklist(definition("a")), Note="n" * 1000)
    with pytest.raises(PreparationError, match="Note is longer than 1000"):
        update(checklist(definition("a")), Note="n" * 1001)


def test_an_unknown_status_is_refused():
    with pytest.raises(PreparationError, match="Status must be one of"):
        update(checklist(definition("a")), Status="done")


def test_an_unknown_status_source_is_refused():
    with pytest.raises(PreparationError, match="StatusSource"):
        update(checklist(definition("a")), source="bot", Status="covered")


def test_an_update_must_change_something():
    with pytest.raises(PreparationError, match="Give a Status"):
        update(checklist(definition("a")))


def test_an_update_to_an_unknown_item_is_refused():
    with pytest.raises(rules.ItemNotFound):
        rules.apply_item_update(
            checklist(definition("a")),
            {"ItemId": "zz", "Status": "covered"},
            status_source="user",
            updated_by="u",
            now=NOW,
            quotes_allowed=True,
        )


def test_a_quote_is_at_most_300_characters():
    update(checklist(definition("a")), Evidence=[{"Quote": "q" * 300}])
    with pytest.raises(PreparationError, match="Quote is longer than 300"):
        update(checklist(definition("a")), Evidence=[{"Quote": "q" * 301}])


@pytest.mark.parametrize("start", [-1, "12", True, float("nan"), float("inf")])
def test_a_start_time_is_a_number_of_seconds(start):
    with pytest.raises(PreparationError, match="StartTime"):
        update(checklist(definition("a")), Evidence=[{"StartTime": start}])


def test_an_evidence_entry_needs_something_to_point_at():
    with pytest.raises(PreparationError, match="needs a SegmentId"):
        update(checklist(definition("a")), Evidence=[{}])


def test_evidence_keeps_the_newest_five():
    items = checklist(definition("a"))
    items, _ = update(items, Evidence=[{"StartTime": t} for t in range(4)])
    _, item = update(items, AddEvidence=[{"StartTime": 10}, {"StartTime": 11}])
    assert [entry["StartTime"] for entry in item["Evidence"]] == [1, 2, 3, 10, 11]
    assert len(item["Evidence"]) == rules.MAX_EVIDENCE_PER_ITEM


def test_evidence_replaces_and_add_evidence_appends():
    items, _ = update(checklist(definition("a")), Evidence=[{"StartTime": 1}])
    items, item = update(items, AddEvidence=[{"StartTime": 2}])
    assert [entry["StartTime"] for entry in item["Evidence"]] == [1, 2]
    _, item = update(items, Evidence=[{"StartTime": 3}])
    assert [entry["StartTime"] for entry in item["Evidence"]] == [3]


def test_after_the_transcript_is_deleted_a_new_quote_is_not_stored():
    _, item = rules.apply_item_update(
        checklist(definition("a")),
        {
            "ItemId": "a",
            "Evidence": [
                {"SegmentId": "s1", "StartTime": 5, "Quote": "said"},
                {"Quote": "only a quote"},
            ],
        },
        status_source="assistant",
        updated_by="LMA",
        now=NOW,
        quotes_allowed=False,
    )
    assert item["Evidence"] == [{"SegmentId": "s1", "StartTime": 5.0, "Quote": None}]


# ── size ─────────────────────────────────────────────────────────────────────


def test_a_full_checklist_of_plain_items_fits():
    items = checklist(*[definition(f"i{i}", "t" * 500, Source="s" * 256) for i in range(100)])
    rules.check_checklist_size(items)


def test_a_checklist_over_128_kb_is_refused():
    items = checklist(*[definition(f"i{i}", "t" * 500, Source="s" * 256) for i in range(100)])
    for item in items:
        item["Note"] = "n" * 1000
        item["Evidence"] = [{"SegmentId": "s" * 128, "StartTime": 1.0, "Quote": "q" * 300}] * 5
    with pytest.raises(PreparationError, match="larger than 128 KB"):
        rules.check_checklist_size(items)


# ── retention ────────────────────────────────────────────────────────────────


def test_deleting_the_transcript_removes_every_quote_and_keeps_where_it_was():
    stripped, changed = rules.strip_quotes(progressed())
    assert changed
    assert stripped[0]["Evidence"] == [{"SegmentId": "s1", "StartTime": 64.5, "Quote": None}]
    assert stripped[0]["Status"] == "covered" and stripped[0]["Note"] == "Agreed"


def test_a_checklist_without_quotes_is_unchanged():
    stripped, changed = rules.strip_quotes(checklist(definition("a")))
    assert not changed and stripped[0]["Evidence"] == []
    assert rules.strip_quotes(None) == ([], False)


# ── {checklist} ──────────────────────────────────────────────────────────────


def test_the_checklist_renders_one_line_per_item_with_its_status():
    items = progressed()
    items, _ = rules.apply_item_update(
        items,
        {"ItemId": "b", "Status": "partial", "AddEvidence": [{"StartTime": 3725}]},
        status_source="assistant",
        updated_by="LMA",
        now=NOW,
        quotes_allowed=True,
    )
    assert rules.render_checklist(items).splitlines() == [
        "1. [covered] (should, question) Who owns the budget? Evidence at 1:04. Note: Agreed",
        "2. [partial] (should, confirm) Confirm the date Evidence at 1:02:05.",
    ]


def test_an_open_item_renders_as_open():
    assert rules.render_checklist(
        checklist(definition("a", "Line one\nline two", Priority="must"))
    ) == ("1. [open] (must, question) Line one line two")


def test_no_checklist_renders_as_nothing():
    assert rules.render_checklist(None) == ""
    assert rules.render_checklist([]) == ""


def test_notes_can_be_left_out():
    assert "Note" not in rules.render_checklist(progressed(), with_notes=False)
