# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The whiteboard rules (no AWS)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lma_whiteboards import rules  # noqa: E402

NOW = "2026-11-20T10:00:00.000Z"
LATER = "2026-11-20T10:05:00.000Z"


def board(**overrides):
    base = rules.new_whiteboard(
        whiteboard_id="wb1",
        title="Plan",
        template="decisions",
        instructions="",
        assistant_updates=True,
        panels=None,
        source=rules.SOURCE_USER,
        actor="owner@example.com",
        now=NOW,
    )
    base.update(overrides)
    return base


def panels_of(b):
    return [
        {"PanelId": p["PanelId"], "Title": p["Title"], "Kind": p["Kind"], "Content": p["Content"]}
        for p in b["Panels"]
    ]


# ── templates and creation ───────────────────────────────────────────────────


def test_a_template_whiteboard_starts_with_the_assistants_empty_panels() -> None:
    b = board()
    assert [p["Title"] for p in b["Panels"]] == ["Decisions", "Open questions", "Notes"]
    assert {p["Source"] for p in b["Panels"]} == {rules.SOURCE_ASSISTANT}
    assert b["Version"] == 1 and b["Title"] == "Plan" and b["AssistantUpdates"] is True


def test_a_whiteboard_with_no_title_takes_its_templates_label() -> None:
    b = rules.new_whiteboard(
        whiteboard_id="wb2",
        title="",
        template="process",
        instructions=None,
        assistant_updates=None,
        panels=None,
        source=rules.SOURCE_USER,
        actor="u",
        now=NOW,
    )
    assert b["Title"] == "Process diagram"
    assert b["Panels"][0]["Kind"] == rules.KIND_MERMAID


def test_panels_a_user_gives_at_creation_are_theirs_unless_handed_over() -> None:
    b = rules.new_whiteboard(
        whiteboard_id="wb3",
        title="T",
        template=None,
        instructions="",
        assistant_updates=True,
        panels=[
            {"Title": "Mine", "Kind": "markdown", "Content": "hello"},
            {"Title": "Fill", "Kind": "markdown", "Source": "assistant"},
        ],
        source=rules.SOURCE_USER,
        actor="u",
        now=NOW,
    )
    assert [p["Source"] for p in b["Panels"]] == ["user", "assistant"]
    assert len({p["PanelId"] for p in b["Panels"]}) == 2


def test_an_unknown_template_is_refused() -> None:
    with pytest.raises(rules.WhiteboardError, match="Template"):
        rules.check_template("canvas")


def test_every_template_is_valid_and_listed() -> None:
    names = [t["name"] for t in rules.template_listing()]
    assert names == list(rules.TEMPLATES)
    assert rules.DEFAULT_TEMPLATE in names
    for name in names:
        rules.new_whiteboard(
            whiteboard_id="x",
            title="",
            template=name,
            instructions="",
            assistant_updates=True,
            panels=None,
            source="user",
            actor="u",
            now=NOW,
        )


# ── limits ───────────────────────────────────────────────────────────────────


def test_a_whiteboard_holds_at_most_twelve_panels() -> None:
    with pytest.raises(rules.WhiteboardError, match="at most 12"):
        rules.panels_input([{"Title": f"P{i}"} for i in range(13)])


def test_panel_ids_are_unique() -> None:
    with pytest.raises(rules.WhiteboardError, match="once"):
        rules.panels_input([{"PanelId": "a", "Title": "A"}, {"PanelId": "a", "Title": "B"}])


def test_a_panel_needs_a_title() -> None:
    with pytest.raises(rules.WhiteboardError, match="title is required"):
        rules.panels_input([{"Title": "  "}])


def test_markdown_and_diagram_panels_have_their_own_length_limits() -> None:
    rules.check_content("markdown", "x" * 8000)
    with pytest.raises(rules.WhiteboardError, match="8000"):
        rules.check_content("markdown", "x" * 8001)
    with pytest.raises(rules.WhiteboardError, match="6000"):
        rules.check_content("mermaid", "flowchart TD\n" + "x" * 6000)


def test_a_whiteboard_is_at_most_32_kb() -> None:
    b = board()
    big = [{"PanelId": f"p{i}", "Title": f"P{i}", "Content": "é" * 4000} for i in range(5)]
    with pytest.raises(rules.WhiteboardError) as caught:
        rules.apply_change(b, source="user", actor="u", now=LATER, panels=big)
    assert caught.value.error_type == "WhiteboardTooLarge"


def test_an_unknown_kind_or_source_is_refused() -> None:
    with pytest.raises(rules.WhiteboardError, match="kind"):
        rules.panels_input([{"Title": "A", "Kind": "canvas"}])
    with pytest.raises(rules.WhiteboardError, match="Source"):
        rules.panels_input([{"Title": "A", "Source": "admin"}])


# ── the Mermaid subset ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "flowchart TD\n  A[Start] --> B{Choice}\n  B -->|yes| C\n  B <--> D",
        "graph LR\n  a --> b",
        "sequenceDiagram\n  Alice->>Bob: Hello",
        "timeline\n  title Launch\n  2026-12 : Beta",
        "mindmap\n  root((Plan))\n    Risks",
        "%% a comment first\nflowchart TD\n  a-->b",
    ],
)
def test_diagrams_in_the_subset_are_accepted(text: str) -> None:
    rules.check_mermaid(text)


@pytest.mark.parametrize(
    "text, message",
    [
        ('pie\n  "a" : 1', "must start with"),
        ("classDiagram\n  A <|-- B", "must start with"),
        ("%%{init: {'theme': 'dark'}}%%\nflowchart TD\n a-->b", "directives"),
        ("---\nconfig:\n  theme: dark\n---\nflowchart TD\n a-->b", "must start with"),
        ("flowchart TD\n  a-->b\n  click a call doThing()", "click"),
        ('flowchart TD\n  a-->b\n  click a "https://example.com"', "click"),
        ("sequenceDiagram\n  link Alice: Site @ https://example.com", "click"),
        ('flowchart TD\n  a["<img src=x>"] --> b', "HTML"),
        ('flowchart TD\n  a["javascript:void"] --> b', "HTML"),
    ],
)
def test_diagrams_outside_the_subset_are_refused(text: str, message: str) -> None:
    with pytest.raises(rules.WhiteboardError, match=message):
        rules.check_mermaid(text)


def test_arrows_that_start_with_a_less_than_sign_are_not_html() -> None:
    rules.check_mermaid("flowchart LR\n  a <-- b\n  c <-.-> d\n  e <== f")


# ── a user's change ──────────────────────────────────────────────────────────


def test_a_panel_a_user_changes_becomes_theirs_and_the_version_moves_on() -> None:
    b = board()
    panels = panels_of(b)
    panels[0]["Content"] = "- Ship in March"
    change = rules.apply_change(b, source="user", actor="bob@example.com", now=LATER, panels=panels)
    assert change.changed and change.changed_panels == ["decisions"]
    stored = change.board["Panels"][0]
    assert stored["Source"] == "user" and stored["UpdatedBy"] == "bob@example.com"
    assert change.board["Panels"][1]["Source"] == "assistant"
    assert change.board["Version"] == 2 and change.board["UpdatedAt"] == LATER


def test_a_panel_resent_without_its_kind_keeps_the_kind_it_has() -> None:
    b = rules.new_whiteboard(
        whiteboard_id="k",
        title="",
        template="process",
        instructions="",
        assistant_updates=True,
        panels=None,
        source="user",
        actor="u",
        now=NOW,
    )
    panels = [
        {"PanelId": p["PanelId"], "Title": p["Title"], "Content": p["Content"]} for p in b["Panels"]
    ]
    panels[0]["Content"] = "flowchart TD\n  a-->b"
    change = rules.apply_change(b, source="user", actor="u", now=LATER, panels=panels)
    assert [p["Kind"] for p in change.board["Panels"]] == ["mermaid", "markdown"]
    # The kept kind is the one the content is checked against.
    panels[0]["Content"] = "flowchart TD\n  click a call x()"
    with pytest.raises(rules.WhiteboardError, match="click"):
        rules.apply_change(b, source="user", actor="u", now=LATER, panels=panels)
    # A new panel without a kind is Markdown.
    panels[0]["Content"] = "flowchart TD\n  a-->b"
    added = rules.apply_change(
        b, source="user", actor="u", now=LATER, panels=panels + [{"Title": "New"}]
    )
    assert added.board["Panels"][-1]["Kind"] == "markdown"


def test_a_user_hands_a_panel_back_to_the_assistant() -> None:
    b = board()
    b["Panels"][0]["Source"] = "user"
    panels = panels_of(b)
    panels[0]["Source"] = "assistant"
    change = rules.apply_change(b, source="user", actor="u", now=LATER, panels=panels)
    assert change.board["Panels"][0]["Source"] == "assistant"
    assert change.changed_panels == ["decisions"]


def test_a_change_that_changes_nothing_keeps_the_version() -> None:
    b = board()
    change = rules.apply_change(
        b, source="user", actor="u", now=LATER, panels=panels_of(b), title="Plan"
    )
    assert not change.changed and change.board["Version"] == 1


def test_a_user_removes_reorders_and_adds_panels() -> None:
    b = board()
    panels = panels_of(b)
    panels = [panels[2], panels[0], {"Title": "Risks", "Content": "- Budget"}]
    change = rules.apply_change(b, source="user", actor="u", now=LATER, panels=panels)
    titles = [p["Title"] for p in change.board["Panels"]]
    assert titles == ["Notes", "Decisions", "Risks"]
    assert change.board["Panels"][2]["Source"] == "user"
    assert "open-questions" in change.changed_panels


def test_a_user_changes_the_title_instructions_and_switch() -> None:
    b = board()
    change = rules.apply_change(
        b,
        source="user",
        actor="u",
        now=LATER,
        title="Pricing",
        instructions="Track prices",
        assistant_updates=False,
    )
    assert change.changed
    assert change.board["Title"] == "Pricing"
    assert change.board["Instructions"] == "Track prices"
    assert change.board["AssistantUpdates"] is False
    assert rules.assistant_panels(change.board) == []


# ── the assistant's change ───────────────────────────────────────────────────


def test_the_assistant_rewrites_its_own_panels_and_appends_new_ones() -> None:
    b = board()
    panels = panels_of(b)
    panels[0]["Content"] = "- Use the March date"
    panels.append({"Title": "Risks", "Content": "- Supplier delay"})
    change = rules.apply_change(
        b, source="assistant", actor=rules.ASSISTANT_LABEL, now=LATER, panels=panels
    )
    assert change.board["Panels"][0]["Source"] == "assistant"
    assert change.board["Panels"][3]["Source"] == "assistant"
    assert len(change.changed_panels) == 2


def test_the_assistant_never_changes_a_panel_a_user_wrote() -> None:
    b = board()
    b["Panels"][0]["Source"] = "user"
    b["Panels"][0]["Content"] = "Mine"
    panels = panels_of(b)
    panels[0]["Content"] = "Overwritten"
    with pytest.raises(rules.WhiteboardError) as caught:
        rules.apply_change(b, source="assistant", actor="a", now=LATER, panels=panels)
    assert caught.value.error_type == "WhiteboardUserPanel"


def test_the_assistant_may_leave_a_users_panel_as_it_is() -> None:
    b = board()
    b["Panels"][0]["Source"] = "user"
    panels = panels_of(b)
    panels[1]["Content"] = "- Who signs off?"
    change = rules.apply_change(b, source="assistant", actor="a", now=LATER, panels=panels)
    assert change.changed_panels == ["open-questions"]
    assert change.board["Panels"][0]["Source"] == "user"


@pytest.mark.parametrize("edit", ["remove", "reorder", "insert", "take"])
def test_the_assistant_may_not_remove_reorder_insert_or_take_panels(edit: str) -> None:
    b = board()
    panels = panels_of(b)
    if edit == "remove":
        panels = panels[:2]
    elif edit == "reorder":
        panels = [panels[1], panels[0], panels[2]]
    elif edit == "insert":
        panels.insert(0, {"Title": "New"})
    else:
        panels[0]["Source"] = "user"
    with pytest.raises(rules.WhiteboardError) as caught:
        rules.apply_change(b, source="assistant", actor="a", now=LATER, panels=panels)
    assert caught.value.error_type == "WhiteboardUserPanel"


def test_the_assistant_changes_nothing_on_a_whiteboard_with_its_updates_off() -> None:
    b = board(AssistantUpdates=False)
    with pytest.raises(rules.WhiteboardError) as caught:
        rules.apply_change(b, source="assistant", actor="a", now=LATER, panels=panels_of(b))
    assert caught.value.error_type == "WhiteboardAssistantOff"


def test_the_assistant_changes_only_panels() -> None:
    with pytest.raises(rules.WhiteboardError):
        rules.apply_change(board(), source="assistant", actor="a", now=LATER, title="Mine")


# ── restoring a version ──────────────────────────────────────────────────────


def test_restoring_a_version_is_a_users_change_of_the_panels_it_changes() -> None:
    v1 = board()
    panels = panels_of(v1)
    panels[0]["Content"] = "- Later decision"
    v2 = rules.apply_change(v1, source="assistant", actor="a", now=LATER, panels=panels).board
    restored = rules.restore(v2, v1, actor="bob@example.com", now=LATER)
    assert restored.changed and restored.board["Version"] == 3
    assert restored.board["Panels"][0]["Content"] == ""
    assert restored.board["Panels"][0]["Source"] == "user"
    assert restored.board["Panels"][1]["Source"] == "assistant"


# ── other forms ──────────────────────────────────────────────────────────────


def test_the_markdown_form_puts_diagrams_in_mermaid_blocks() -> None:
    b = rules.new_whiteboard(
        whiteboard_id="m",
        title="Flow",
        template="process",
        instructions="",
        assistant_updates=True,
        panels=[
            {"Title": "Diagram", "Kind": "mermaid", "Content": "flowchart TD\n  a-->b"},
            {"Title": "Notes", "Content": "- one"},
        ],
        source="user",
        actor="u",
        now=NOW,
    )
    text = rules.whiteboard_markdown(b)
    assert text.startswith("# Flow\n")
    assert "```mermaid\nflowchart TD\n  a-->b\n```" in text
    assert "## Notes\n\n- one" in text


def test_the_camel_case_form_names_every_field() -> None:
    out = rules.whiteboard_output(board())
    assert set(out) == {
        "whiteboardId",
        "title",
        "template",
        "instructions",
        "assistantUpdates",
        "version",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
        "panels",
    }
    assert set(out["panels"][0]) == {
        "panelId",
        "title",
        "kind",
        "content",
        "source",
        "updatedBy",
        "updatedAt",
    }


def test_settings_take_the_shipped_values_and_bounds() -> None:
    s = rules.Settings.from_raw({})
    assert (s.default_enabled, s.interval_seconds, s.min_new_segments, s.max_runs_per_meeting) == (
        False,
        180,
        15,
        60,
    )
    assert s.max_whiteboards == 5 and s.assistant_available
    s = rules.Settings.from_raw(
        {
            "intervalSeconds": 5,
            "maxRunsPerMeeting": 0,
            "maxWhiteboardsPerMeeting": 99,
            "defaultEnabled": "yes",
        }
    )
    assert s.interval_seconds == 60 and not s.assistant_available
    assert s.max_whiteboards == rules.HARD_MAX_WHITEBOARDS and s.default_enabled is False


def test_versions_sort_by_number() -> None:
    keys = [rules.version_key("wb", n) for n in (10, 2, 1)]
    assert sorted(keys) == [rules.version_key("wb", n) for n in (1, 2, 10)]
