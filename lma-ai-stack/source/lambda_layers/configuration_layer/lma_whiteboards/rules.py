#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The rules of a meeting's whiteboards, with no AWS in them.

A meeting has up to ``MAX_WHITEBOARDS`` whiteboards. Each is an ordered list of
up to ``MAX_PANELS`` named panels, and each panel is Markdown or a diagram in a
restricted subset of Mermaid. Each panel records who it belongs to:

``user``
    A person wrote or changed it, on the meeting page or through an MCP client
    acting for them. The assistant never changes it again; only a user can hand
    it back.
``assistant``
    The assistant may rewrite it on each run.

Everything that decides something about a whiteboard lives here -- every limit,
the templates, the Mermaid subset, the merge that applies a change and records
who each panel belongs to, the refusal of an assistant change to a user's
panel, and the Markdown and camelCase forms -- so the resolver that writes them,
the assistant that updates them, the MCP tools and the meeting export all
decide alike. See docs/meeting-whiteboards.md.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Kept free of boto3 and of lma_config so that any function, test or script can
# import the rules.

#: The most whiteboards a meeting may have; the setting is at most this.
HARD_MAX_WHITEBOARDS = 10
#: The shipped "Whiteboards per meeting".
DEFAULT_MAX_WHITEBOARDS = 5
MAX_PANELS = 12
MAX_TITLE_CHARS = 120
MAX_PANEL_TITLE_CHARS = 80
MAX_INSTRUCTIONS_CHARS = 1000
MAX_MARKDOWN_CHARS = 8000
MAX_MERMAID_CHARS = 6000
#: A whiteboard as stored (title, instructions and panels, as UTF-8 JSON).
MAX_WHITEBOARD_BYTES = 32 * 1024
MAX_ATTRIBUTION_CHARS = 128

ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}")

KIND_MARKDOWN = "markdown"
KIND_MERMAID = "mermaid"
KINDS = (KIND_MARKDOWN, KIND_MERMAID)

SOURCE_USER = "user"
SOURCE_ASSISTANT = "assistant"
SOURCES = (SOURCE_USER, SOURCE_ASSISTANT)

#: What an assistant change is recorded as made by.
ASSISTANT_LABEL = "LMA assistant"

#: The Mermaid diagram types a panel may hold, by the keyword that starts them.
MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "timeline", "mindmap")


class WhiteboardError(ValueError):
    """A change the rules refuse, with the error type the API reports it as."""

    def __init__(self, message: str, error_type: str = "WhiteboardInvalid") -> None:
        super().__init__(message)
        self.error_type = error_type


def _invalid(message: str) -> WhiteboardError:
    return WhiteboardError(message, "WhiteboardInvalid")


# ── templates ────────────────────────────────────────────────────────────────


def _panel(panel_id: str, title: str, kind: str = KIND_MARKDOWN) -> Dict[str, str]:
    return {"PanelId": panel_id, "Title": title, "Kind": kind, "Content": ""}


#: The shipped templates: a label, what it is for (shown when choosing one, and
#: given to the assistant), and the panels a new whiteboard starts with.
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "decisions": {
        "label": "Decisions and open questions",
        "description": "What was decided, what is still open, and notes worth keeping.",
        "panels": [
            _panel("decisions", "Decisions"),
            _panel("open-questions", "Open questions"),
            _panel("notes", "Notes"),
        ],
    },
    "process": {
        "label": "Process diagram",
        "description": "A flowchart of the process or system being discussed, with notes.",
        "panels": [_panel("diagram", "Diagram", KIND_MERMAID), _panel("notes", "Notes")],
    },
    "comparison": {
        "label": "Comparison",
        "description": "The options being compared, as a Markdown table, with notes.",
        "panels": [_panel("comparison", "Comparison"), _panel("notes", "Notes")],
    },
    "timeline": {
        "label": "Timeline",
        "description": "Dates and milestones mentioned, as a Mermaid timeline, with notes.",
        "panels": [_panel("timeline", "Timeline", KIND_MERMAID), _panel("notes", "Notes")],
    },
    "blank": {
        "label": "Blank",
        "description": "One Markdown panel to start from.",
        "panels": [_panel("notes", "Notes")],
    },
}
DEFAULT_TEMPLATE = "decisions"


def check_template(value: Any) -> str:
    """A template name, or the default for an empty one."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_TEMPLATE
    if not isinstance(value, str) or value.strip() not in TEMPLATES:
        raise _invalid(f"Template must be one of: {', '.join(TEMPLATES)}")
    return value.strip()


def template_listing() -> List[Dict[str, Any]]:
    """The templates, for a page or a client to offer."""
    return [
        {
            "name": name,
            "label": template["label"],
            "description": template["description"],
            "panels": [{"title": p["Title"], "kind": p["Kind"]} for p in template["panels"]],
        }
        for name, template in TEMPLATES.items()
    ]


# ── single values ────────────────────────────────────────────────────────────


def check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise _invalid(f"{what} must be 1 to 64 letters, digits, '.', '_', ':' and '-'")
    return value


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def check_text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    """A text value: trimmed, at most ``limit`` characters; required ones not empty."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _invalid(f"{what} must be text")
    text = value.strip()
    if required and not text:
        raise _invalid(f"{what} is required")
    if len(text) > limit:
        raise _invalid(f"{what} is at most {limit} characters")
    return text


def check_mermaid(text: str) -> None:
    """Refuse a diagram outside the subset a panel may hold.

    The subset: a diagram whose first line names one of MERMAID_TYPES, with no
    directives or front matter (which change how it is rendered), no click
    handlers, callbacks or links (which make it interactive), and no HTML tags.
    The page renders it in Mermaid's strict mode as well.
    """
    lines = [line.strip() for line in text.splitlines()]
    meaningful = [line for line in lines if line and not line.startswith("%%")]
    if not meaningful:
        return
    first = meaningful[0]
    if not any(first == kind or first.startswith(kind + " ") for kind in MERMAID_TYPES):
        raise _invalid(
            "A diagram must start with one of: " + ", ".join(MERMAID_TYPES) + " (Mermaid)"
        )
    lowered = text.lower()
    if "%%{" in text or any(line == "---" for line in lines):
        raise _invalid("A diagram may not hold Mermaid directives or front matter")
    for line in meaningful:
        word = line.split(None, 1)[0].lower() if line.split() else ""
        if word in ("click", "callback", "link", "links", "href"):
            raise _invalid("A diagram may not hold click handlers, callbacks or links")
    if "javascript:" in lowered or re.search(r"<\s*/?\s*[a-z!]", lowered):
        raise _invalid("A diagram may not hold HTML")


def check_content(kind: str, value: Any) -> str:
    """A panel's content for its kind: Markdown up to 8000 characters, a diagram up to 6000."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _invalid("A panel's content must be text")
    text = value.replace("\r\n", "\n").strip("\n")
    if kind == KIND_MERMAID:
        if len(text) > MAX_MERMAID_CHARS:
            raise _invalid(f"A diagram panel is at most {MAX_MERMAID_CHARS} characters")
        check_mermaid(text)
    elif len(text) > MAX_MARKDOWN_CHARS:
        raise _invalid(f"A Markdown panel is at most {MAX_MARKDOWN_CHARS} characters")
    return text


def check_kind(value: Any) -> str:
    if value is None or value == "":
        return KIND_MARKDOWN
    if value not in KINDS:
        raise _invalid(f"A panel's kind must be one of: {', '.join(KINDS)}")
    return str(value)


def check_source(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if value not in SOURCES:
        raise _invalid(f"Source must be one of: {', '.join(SOURCES)}")
    return str(value)


def attribution(value: Any, default: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return (text or default)[:MAX_ATTRIBUTION_CHARS]


# ── panels and whiteboards ───────────────────────────────────────────────────


def panel_input(raw: Any, index: int) -> Dict[str, Any]:
    """One panel as a caller sent it: ``PanelId`` (optional), ``Title``, ``Kind``,
    ``Content`` and an optional ``Source``. Checked, not yet merged."""
    if not isinstance(raw, Mapping):
        raise _invalid(f"Panel {index + 1} must be an object")
    kind = check_kind(raw.get("Kind"))
    panel_id = raw.get("PanelId")
    return {
        "PanelId": check_id(panel_id, f"Panel {index + 1}'s PanelId") if panel_id else None,
        "Title": check_text(
            raw.get("Title"), f"Panel {index + 1}'s title", MAX_PANEL_TITLE_CHARS, required=True
        ),
        "Kind": kind,
        "Content": check_content(kind, raw.get("Content")),
        "Source": check_source(raw.get("Source")),
    }


def panels_input(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        raise _invalid("Panels must be a list")
    if len(raw) > MAX_PANELS:
        raise _invalid(f"A whiteboard has at most {MAX_PANELS} panels")
    panels = [panel_input(item, index) for index, item in enumerate(raw)]
    ids = [p["PanelId"] for p in panels if p["PanelId"]]
    if len(ids) != len(set(ids)):
        raise _invalid("Each panel's PanelId is used once")
    return panels


def whiteboard_bytes(board: Mapping[str, Any]) -> int:
    payload = {
        "Title": board.get("Title"),
        "Instructions": board.get("Instructions"),
        "Panels": board.get("Panels"),
    }
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _check_size(board: Mapping[str, Any]) -> None:
    if whiteboard_bytes(board) > MAX_WHITEBOARD_BYTES:
        raise WhiteboardError(
            f"A whiteboard is at most {MAX_WHITEBOARD_BYTES // 1024} KB", "WhiteboardTooLarge"
        )


def _unique_id(wanted: Optional[str], taken: set) -> str:
    panel_id = wanted or new_id()
    while panel_id in taken:
        panel_id = new_id()
    taken.add(panel_id)
    return panel_id


def new_whiteboard(  # pylint: disable=too-many-arguments
    *,
    whiteboard_id: str,
    title: Any,
    template: Any,
    instructions: Any,
    assistant_updates: Any,
    panels: Any,
    source: str,
    actor: str,
    now: str,
) -> Dict[str, Any]:
    """A new whiteboard, version 1: the given panels, else the template's."""
    template_name = check_template(template)
    chosen = TEMPLATES[template_name]
    given = panels_input(panels) if panels else None
    taken: set = set()
    stored = []
    for panel in given if given is not None else chosen["panels"]:
        stored.append(
            {
                "PanelId": _unique_id(panel.get("PanelId"), taken),
                "Title": panel["Title"],
                "Kind": panel["Kind"],
                "Content": panel.get("Content") or "",
                # A template's empty panels are the assistant's to fill; a panel
                # a person wrote is theirs unless they hand it over.
                "Source": (panel.get("Source") or source)
                if given is not None
                else SOURCE_ASSISTANT,
                "UpdatedBy": actor,
                "UpdatedAt": now,
            }
        )
    board = {
        "WhiteboardId": check_id(whiteboard_id, "WhiteboardId"),
        "Title": check_text(title, "Title", MAX_TITLE_CHARS) or chosen["label"],
        "Template": template_name,
        "Instructions": check_text(instructions, "Instructions", MAX_INSTRUCTIONS_CHARS),
        "AssistantUpdates": assistant_updates is not False,
        "Panels": stored,
        "Version": 1,
        "CreatedBy": actor,
        "CreatedAt": now,
        "UpdatedBy": actor,
        "UpdatedAt": now,
    }
    _check_size(board)
    return board


def _same_content(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (
        a.get("Title") == b.get("Title")
        and a.get("Kind") == b.get("Kind")
        and (a.get("Content") or "") == (b.get("Content") or "")
    )


@dataclass
class Change:
    """The result of applying a change: the whiteboard as it would be stored,
    the panels it changed, and whether it changed anything at all."""

    board: Dict[str, Any]
    changed_panels: List[str]
    changed: bool


def apply_change(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
    board: Mapping[str, Any],
    *,
    source: str,
    actor: str,
    now: str,
    title: Any = None,
    instructions: Any = None,
    assistant_updates: Any = None,
    panels: Any = None,
) -> Change:
    """Apply a change to a stored whiteboard, by a user or by the assistant.

    ``panels`` is the whole new list, in order. For a **user**: a panel whose
    title, kind or content changed becomes theirs (``Source: user``) unless it
    is sent with ``Source: assistant``, which hands it to the assistant; a
    panel sent with only a different Source changes hands. Panels left out are
    removed. For the **assistant**: it may rewrite its own panels and add new
    ones after the existing panels; it may not remove, reorder or change a
    user's panel, change a panel's owner, or change the title, instructions or
    switch, and it may not change a whiteboard whose assistant updates are off.
    Such a change is refused as a whole (``WhiteboardUserPanel`` /
    ``WhiteboardAssistantOff``), with nothing applied.

    A change that changes nothing leaves the version as it is.
    """
    if source not in SOURCES:
        raise _invalid(f"Source must be one of: {', '.join(SOURCES)}")
    stored = [dict(p) for p in board.get("Panels") or []]
    by_id = {p["PanelId"]: p for p in stored}
    result = dict(board)
    changed_panels: List[str] = []
    meta_changed = False

    if source == SOURCE_ASSISTANT:
        if board.get("AssistantUpdates") is False:
            raise WhiteboardError(
                "The assistant does not update this whiteboard", "WhiteboardAssistantOff"
            )
        if title is not None or instructions is not None or assistant_updates is not None:
            raise WhiteboardError(
                "The assistant changes only a whiteboard's panels", "WhiteboardUserPanel"
            )
    else:
        if title is not None:
            new_title = check_text(title, "Title", MAX_TITLE_CHARS, required=True)
            meta_changed |= new_title != board.get("Title")
            result["Title"] = new_title
        if instructions is not None:
            text = check_text(instructions, "Instructions", MAX_INSTRUCTIONS_CHARS)
            meta_changed |= text != (board.get("Instructions") or "")
            result["Instructions"] = text
        if assistant_updates is not None:
            flag = assistant_updates is True
            meta_changed |= flag != (board.get("AssistantUpdates") is not False)
            result["AssistantUpdates"] = flag

    if panels is not None:
        given = panels_input(panels)
        if source == SOURCE_ASSISTANT:
            given_ids = [p["PanelId"] for p in given if p["PanelId"] in by_id]
            stored_ids = [p["PanelId"] for p in stored]
            if given_ids != stored_ids:
                raise WhiteboardError(
                    "The assistant may not remove or reorder panels", "WhiteboardUserPanel"
                )
            seen_new = False
            for panel in given:
                if panel["PanelId"] in by_id:
                    if seen_new:
                        raise WhiteboardError(
                            "The assistant adds panels after the existing ones",
                            "WhiteboardUserPanel",
                        )
                else:
                    seen_new = True
        taken = set(by_id)
        merged: List[Dict[str, Any]] = []
        for panel in given:
            old = by_id.get(panel["PanelId"]) if panel["PanelId"] else None
            if old is None:
                owner = (
                    SOURCE_ASSISTANT
                    if source == SOURCE_ASSISTANT
                    else (panel["Source"] or SOURCE_USER)
                )
                entry = {
                    "PanelId": _unique_id(panel["PanelId"], taken),
                    "Title": panel["Title"],
                    "Kind": panel["Kind"],
                    "Content": panel["Content"],
                    "Source": owner,
                    "UpdatedBy": actor,
                    "UpdatedAt": now,
                }
                changed_panels.append(entry["PanelId"])
                merged.append(entry)
                continue
            same = _same_content(old, panel)
            if source == SOURCE_ASSISTANT:
                if old.get("Source") != SOURCE_ASSISTANT and not same:
                    raise WhiteboardError(
                        f"The panel {old.get('Title')!r} was written by a user, and only a "
                        "user changes it",
                        "WhiteboardUserPanel",
                    )
                if panel["Source"] not in (None, old.get("Source")):
                    raise WhiteboardError(
                        "The assistant may not change who a panel belongs to",
                        "WhiteboardUserPanel",
                    )
                owner = old.get("Source")
            elif not same:
                owner = panel["Source"] or SOURCE_USER
            else:
                owner = panel["Source"] or old.get("Source") or SOURCE_USER
            if same and owner == old.get("Source"):
                merged.append(old)
                continue
            entry = {
                **old,
                "Title": panel["Title"],
                "Kind": panel["Kind"],
                "Content": panel["Content"],
                "Source": owner,
                "UpdatedBy": actor,
                "UpdatedAt": now,
            }
            changed_panels.append(entry["PanelId"])
            merged.append(entry)
        removed = [
            p["PanelId"] for p in stored if p["PanelId"] not in {m["PanelId"] for m in merged}
        ]
        order_changed = [m["PanelId"] for m in merged if m["PanelId"] in by_id] != [
            p["PanelId"] for p in stored if p["PanelId"] not in removed
        ]
        changed_panels.extend(removed)
        meta_changed |= order_changed
        result["Panels"] = merged

    changed = meta_changed or bool(changed_panels)
    if changed:
        result["Version"] = int(board.get("Version") or 0) + 1
        result["UpdatedBy"] = actor
        result["UpdatedAt"] = now
    _check_size(result)
    return Change(board=result, changed_panels=changed_panels, changed=changed)


def restore(
    board: Mapping[str, Any], snapshot: Mapping[str, Any], *, actor: str, now: str
) -> Change:
    """Bring back an earlier version's title and panels, as the user's change.

    The panels it changes become the user's, as any edit does; the ones it
    leaves as they are keep their owner.
    """
    panels = [
        {
            "PanelId": p.get("PanelId"),
            "Title": p.get("Title"),
            "Kind": p.get("Kind"),
            "Content": p.get("Content") or "",
        }
        for p in snapshot.get("Panels") or []
    ]
    return apply_change(
        board,
        source=SOURCE_USER,
        actor=actor,
        now=now,
        title=snapshot.get("Title"),
        panels=panels,
    )


def assistant_panels(board: Mapping[str, Any]) -> List[str]:
    """The ids of the panels the assistant may rewrite."""
    if board.get("AssistantUpdates") is False:
        return []
    return [p["PanelId"] for p in board.get("Panels") or [] if p.get("Source") == SOURCE_ASSISTANT]


# ── other forms ──────────────────────────────────────────────────────────────


def panel_output(panel: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "panelId": panel.get("PanelId"),
        "title": panel.get("Title"),
        "kind": panel.get("Kind"),
        "content": panel.get("Content") or "",
        "source": panel.get("Source"),
        "updatedBy": panel.get("UpdatedBy"),
        "updatedAt": panel.get("UpdatedAt"),
    }


def whiteboard_output(board: Mapping[str, Any], *, panels: bool = True) -> Dict[str, Any]:
    """A whiteboard in the camelCase form of the MCP tools and the meeting export."""
    out = {
        "whiteboardId": board.get("WhiteboardId"),
        "title": board.get("Title"),
        "template": board.get("Template"),
        "instructions": board.get("Instructions") or "",
        "assistantUpdates": board.get("AssistantUpdates") is not False,
        "version": _int(board.get("Version")),
        "createdBy": board.get("CreatedBy"),
        "createdAt": board.get("CreatedAt"),
        "updatedBy": board.get("UpdatedBy"),
        "updatedAt": board.get("UpdatedAt"),
    }
    if panels:
        out["panels"] = [panel_output(p) for p in board.get("Panels") or []]
    return out


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def whiteboard_markdown(board: Mapping[str, Any]) -> str:
    """A whiteboard as one Markdown document, diagrams in ``mermaid`` code blocks."""
    parts = [f"# {board.get('Title') or 'Whiteboard'}", ""]
    for panel in board.get("Panels") or []:
        parts += [f"## {panel.get('Title')}", ""]
        content = (panel.get("Content") or "").strip()
        if panel.get("Kind") == KIND_MERMAID:
            parts += ["```mermaid", content, "```"] if content else ["_(empty diagram)_"]
        else:
            parts.append(content or "_(empty)_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def sort_whiteboards(boards: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Whiteboards in the order they were created."""
    return sorted(
        (dict(b) for b in boards),
        key=lambda b: (str(b.get("CreatedAt") or ""), str(b.get("WhiteboardId"))),
    )


# ── settings ─────────────────────────────────────────────────────────────────


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


@dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    """Meeting Assistant › Whiteboards, with the shipped values for any that are missing."""

    default_enabled: bool = False
    interval_seconds: int = 180
    min_new_segments: int = 15
    max_runs_per_meeting: int = 60
    max_whiteboards: int = DEFAULT_MAX_WHITEBOARDS
    model_id: str = ""
    prompt: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Settings":
        raw = raw if isinstance(raw, Mapping) else {}
        model_id = raw.get("modelId")
        prompt = raw.get("prompt")
        return cls(
            default_enabled=raw.get("defaultEnabled") is True,
            interval_seconds=_bounded_int(raw.get("intervalSeconds"), 180, 60, 3600),
            min_new_segments=_bounded_int(raw.get("minNewSegments"), 15, 1, 500),
            max_runs_per_meeting=_bounded_int(raw.get("maxRunsPerMeeting"), 60, 0, 1000),
            max_whiteboards=_bounded_int(
                raw.get("maxWhiteboardsPerMeeting"),
                DEFAULT_MAX_WHITEBOARDS,
                1,
                HARD_MAX_WHITEBOARDS,
            ),
            model_id=model_id.strip() if isinstance(model_id, str) else "",
            prompt=prompt.strip() if isinstance(prompt, str) else "",
        )

    @property
    def assistant_available(self) -> bool:
        """False when an administrator has turned the assistant's updates off for the deployment."""
        return self.max_runs_per_meeting > 0


def version_key(whiteboard_id: str, version: int) -> str:
    """The sort key of a stored version, ordered by number."""
    return f"v#{whiteboard_id}#{int(version):06d}"


def head_key(whiteboard_id: str) -> str:
    return f"b#{whiteboard_id}"


def partition(call_id: str) -> str:
    """Every row of a meeting's whiteboards is under this partition key."""
    return f"wb#{call_id}"


def changed_titles(board: Mapping[str, Any], panel_ids: Sequence[str]) -> List[str]:
    titles = {p.get("PanelId"): p.get("Title") for p in board.get("Panels") or []}
    return [str(titles.get(pid) or pid) for pid in panel_ids]


__all__ = [
    "ASSISTANT_LABEL",
    "Change",
    "DEFAULT_TEMPLATE",
    "KINDS",
    "KIND_MARKDOWN",
    "KIND_MERMAID",
    "MAX_PANELS",
    "MERMAID_TYPES",
    "SOURCES",
    "SOURCE_ASSISTANT",
    "SOURCE_USER",
    "Settings",
    "TEMPLATES",
    "WhiteboardError",
    "apply_change",
    "assistant_panels",
    "check_content",
    "check_id",
    "check_mermaid",
    "check_template",
    "head_key",
    "new_id",
    "new_whiteboard",
    "partition",
    "restore",
    "sort_whiteboards",
    "template_listing",
    "version_key",
    "whiteboard_markdown",
    "whiteboard_output",
]
