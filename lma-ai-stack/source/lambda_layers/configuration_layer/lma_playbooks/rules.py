#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The rules of a meeting playbook, with no AWS in them.

A **playbook** is a skill (docs/meeting-assistant-skills.md) whose front matter
has a ``playbook`` section, kept as the skill's metadata like the other
front-matter fields, so the Skills page, ``SKILL.md`` and the skill library carry
it unchanged. The section configures a meeting when the playbook is applied to
it:

.. code-block:: yaml

    playbook:
      suggestFor: [sprint planning, iteration planning]
      whiteboards:
        - template: user_stories
          title: Sprint backlog
          instructions: One story per item the team commits to.
          assistantUpdates: true
      guidance:
        checks: [action_missing_owner, action_missing_due_date, risk_probe]
        assertiveness: balanced
        maxItemsPer10Min: 6
      quickActions:
        - label: Stories so far
          prompt: List the stories agreed so far with their estimates and owners.

The skill's instructions are the playbook's guidance for the meeting assistant,
given with requests made about a meeting the playbook is applied to.

Everything that decides something about a playbook lives here -- the names it
may use, its limits, reading it strictly (when an Admin saves it) and leniently
(when it is applied, so that a playbook stored by an older or newer release
applies what it can), the suggestion from a meeting's title and agenda, the
live guidance settings a meeting runs with, and the cost estimate -- so the
Skills Manager, the function that applies a playbook and live guidance decide
alike. See docs/meeting-playbooks.md.
"""

from __future__ import annotations

import math
import numbers
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Kept free of boto3 and of lma_config so that any function, test or script can
# import the rules. The whiteboard templates are the whiteboard rules' own.
from lma_whiteboards import rules as whiteboard_rules

#: The front-matter field, and the key in a skill's metadata, that holds a playbook.
PLAYBOOK_KEY = "playbook"

#: The most whiteboards a playbook creates; a meeting's own cap applies too.
MAX_WHITEBOARDS = 5
MAX_QUICK_ACTIONS = 6
MAX_QUICK_ACTION_LABEL_CHARS = 40
MAX_QUICK_ACTION_PROMPT_CHARS = 1000
MAX_SUGGEST_PHRASES = 20
MAX_SUGGEST_PHRASE_CHARS = 60
MAX_WHITEBOARD_TITLE_CHARS = whiteboard_rules.MAX_TITLE_CHARS
MAX_WHITEBOARD_INSTRUCTIONS_CHARS = whiteboard_rules.MAX_INSTRUCTIONS_CHARS
#: How assertive a playbook's guidance is: the confidence threshold an item
#: must reach, in place of the deployment's. Words rather than a number, so a
#: playbook is stored and exported without a decimal value in it.
ASSERTIVENESS: Dict[str, float] = {"quiet": 0.85, "balanced": 0.75, "assertive": 0.65}
DEFAULT_ASSERTIVENESS = "balanced"
#: The bounds of a playbook's own item rate.
MIN_ITEMS_PER_10_MIN, MAX_ITEMS_PER_10_MIN = 1, 20

#: The live guidance checks a playbook may turn on, with the label the pages
#: show. The names are the guidance types live guidance publishes
#: (docs/live-guidance.md); the digest is made only on request and is not one.
GUIDANCE_CHECKS: Dict[str, str] = {
    "ask_question": "Questions to ask",
    "uncovered_topic": "Topics not yet covered",
    "clarify": "Things to clarify",
    "risk_probe": "Risks to probe",
    "action_missing_owner": "Actions without an owner",
    "action_missing_due_date": "Actions without a due date",
    "time_check": "Time checks",
    "fact_check": "Fact checks",
    "lookup": "Lookups",
}
#: The checks that research the deployment's sources; each also needs the
#: deployment's own switch (Fact checks, Lookups) on.
RESEARCH_CHECKS: Dict[str, str] = {"fact_check": "factChecksEnabled", "lookup": "lookupsEnabled"}
#: Guidance types that are not checks and that a playbook never turns off.
ALWAYS_ALLOWED_TYPES = ("meeting_digest",)

_PLAYBOOK_KEYS = ("suggestFor", "whiteboards", "guidance", "quickActions")
_WHITEBOARD_KEYS = ("template", "title", "instructions", "assistantUpdates")
_GUIDANCE_KEYS = ("checks", "assertiveness", "maxItemsPer10Min")
_QUICK_ACTION_KEYS = ("label", "prompt")

#: Characters a label, title or phrase may not hold: controls other than
#: whitespace, which no page shows meaningfully.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PlaybookError(ValueError):
    """A playbook that cannot be stored, with every problem found in it."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def template_names() -> List[str]:
    return list(whiteboard_rules.TEMPLATES)


def _text(value: Any, what: str, limit: int, problems: List[str], *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        problems.append(f"{what} must be text")
        return ""
    text = _CONTROL.sub("", value).strip()
    if required and not text:
        problems.append(f"{what} is required")
    if len(text) > limit:
        problems.append(f"{what} is longer than {limit} characters")
        text = text[:limit]
    return text


def _unknown_keys(raw: Mapping[str, Any], allowed: Sequence[str], where: str) -> List[str]:
    return [f"{where} has no field '{key}'" for key in raw if key not in allowed]


def _number(value: Any) -> Optional[float]:
    # A number, including DynamoDB's Decimal; never a boolean.
    if isinstance(value, bool) or not isinstance(value, numbers.Number):
        return None
    number = float(value)  # type: ignore[arg-type]
    return number if math.isfinite(number) else None


def _whiteboard(raw: Any, index: int, problems: List[str]) -> Optional[Dict[str, Any]]:
    where = f"whiteboards[{index + 1}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where} must be a map with a template")
        return None
    problems.extend(_unknown_keys(raw, _WHITEBOARD_KEYS, where))
    template = raw.get("template")
    if not isinstance(template, str) or template.strip() not in whiteboard_rules.TEMPLATES:
        problems.append(
            f"{where}: template '{template}' is not a whiteboard template; "
            f"use one of: {', '.join(whiteboard_rules.TEMPLATES)}"
        )
        return None
    template = template.strip()
    title = _text(
        raw.get("title"), f"{where} title", MAX_WHITEBOARD_TITLE_CHARS, problems, required=False
    )
    instructions = _text(
        raw.get("instructions"),
        f"{where} instructions",
        MAX_WHITEBOARD_INSTRUCTIONS_CHARS,
        problems,
        required=False,
    )
    updates = raw.get("assistantUpdates", True)
    if not isinstance(updates, bool):
        problems.append(f"{where} assistantUpdates must be true or false")
        updates = True
    return {
        "template": template,
        "title": title or str(whiteboard_rules.TEMPLATES[template]["label"]),
        "instructions": instructions,
        "assistantUpdates": updates,
    }


def _guidance(raw: Any, problems: List[str]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        problems.append("guidance must be a map with checks")
        return None
    problems.extend(_unknown_keys(raw, _GUIDANCE_KEYS, "guidance"))
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list):
        problems.append("guidance checks must be a list of check names")
        checks_raw = []
    checks: List[str] = []
    for check in checks_raw:
        if not isinstance(check, str) or check.strip() not in GUIDANCE_CHECKS:
            problems.append(
                f"guidance check '{check}' is not a live guidance check; "
                f"use any of: {', '.join(GUIDANCE_CHECKS)}"
            )
        elif check.strip() not in checks:
            checks.append(check.strip())
    out: Dict[str, Any] = {"checks": checks}
    assertiveness = raw.get("assertiveness", DEFAULT_ASSERTIVENESS)
    if not isinstance(assertiveness, str) or assertiveness.strip() not in ASSERTIVENESS:
        problems.append(
            f"guidance assertiveness '{assertiveness}' is not one of: {', '.join(ASSERTIVENESS)}"
        )
        assertiveness = DEFAULT_ASSERTIVENESS
    out["assertiveness"] = assertiveness.strip()
    if "maxItemsPer10Min" in raw:
        value = _number(raw.get("maxItemsPer10Min"))
        if (
            value is None
            or value != int(value)
            or not MIN_ITEMS_PER_10_MIN <= value <= MAX_ITEMS_PER_10_MIN
        ):
            problems.append(
                f"guidance maxItemsPer10Min must be a whole number from "
                f"{MIN_ITEMS_PER_10_MIN} to {MAX_ITEMS_PER_10_MIN}"
            )
        else:
            out["maxItemsPer10Min"] = int(value)
    return out


def _quick_action(raw: Any, index: int, problems: List[str]) -> Optional[Dict[str, str]]:
    where = f"quickActions[{index + 1}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where} must be a map with a label and a prompt")
        return None
    problems.extend(_unknown_keys(raw, _QUICK_ACTION_KEYS, where))
    label = _text(
        raw.get("label"), f"{where} label", MAX_QUICK_ACTION_LABEL_CHARS, problems, required=True
    )
    prompt = _text(
        raw.get("prompt"), f"{where} prompt", MAX_QUICK_ACTION_PROMPT_CHARS, problems, required=True
    )
    if not label or not prompt:
        return None
    return {"label": label, "prompt": prompt}


def _read(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """The playbook ``raw`` describes, and every problem found in it."""
    problems: List[str] = []
    if not isinstance(raw, Mapping):
        return {}, ["the playbook section must be a map"]
    problems.extend(_unknown_keys(raw, _PLAYBOOK_KEYS, "the playbook"))

    phrases_raw = raw.get("suggestFor") or []
    if not isinstance(phrases_raw, list):
        problems.append("suggestFor must be a list of phrases")
        phrases_raw = []
    if len(phrases_raw) > MAX_SUGGEST_PHRASES:
        problems.append(f"suggestFor lists more than {MAX_SUGGEST_PHRASES} phrases")
        phrases_raw = phrases_raw[:MAX_SUGGEST_PHRASES]
    phrases: List[str] = []
    for index, phrase in enumerate(phrases_raw):
        text = _text(
            phrase, f"suggestFor[{index + 1}]", MAX_SUGGEST_PHRASE_CHARS, problems, required=True
        )
        if text and _words(text) and text not in phrases:
            phrases.append(text)

    boards_raw = raw.get("whiteboards") or []
    if not isinstance(boards_raw, list):
        problems.append("whiteboards must be a list")
        boards_raw = []
    if len(boards_raw) > MAX_WHITEBOARDS:
        problems.append(f"a playbook creates at most {MAX_WHITEBOARDS} whiteboards")
        boards_raw = boards_raw[:MAX_WHITEBOARDS]
    boards = [b for i, raw_b in enumerate(boards_raw) if (b := _whiteboard(raw_b, i, problems))]

    guidance = _guidance(raw.get("guidance"), problems)

    actions_raw = raw.get("quickActions") or []
    if not isinstance(actions_raw, list):
        problems.append("quickActions must be a list")
        actions_raw = []
    if len(actions_raw) > MAX_QUICK_ACTIONS:
        problems.append(f"a playbook has at most {MAX_QUICK_ACTIONS} quick actions")
        actions_raw = actions_raw[:MAX_QUICK_ACTIONS]
    actions = [a for i, raw_a in enumerate(actions_raw) if (a := _quick_action(raw_a, i, problems))]

    playbook: Dict[str, Any] = {"suggestFor": phrases, "whiteboards": boards}
    if guidance is not None:
        playbook["guidance"] = guidance
    playbook["quickActions"] = actions
    return playbook, problems


def plain(value: Any) -> Any:
    """``value`` with DynamoDB's Decimals as ints or floats, recursively."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, numbers.Number) and not isinstance(value, (int, float)):
        number = float(value)  # type: ignore[arg-type]
        return int(number) if number.is_integer() else number
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def parse_playbook(raw: Any) -> Dict[str, Any]:
    """A playbook as it is stored, for an Admin's save or an import.

    Raises:
        PlaybookError: listing every problem, so the editor can show them all.
    """
    playbook, problems = _read(raw)
    if problems:
        raise PlaybookError(problems)
    return playbook


def lenient_playbook(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """What can be applied of a stored playbook, and a note of each part left out.

    A whiteboard with an unknown template, a check this release does not know, a
    value out of range: each is left out and noted, and the rest is applied.
    """
    return _read(raw)


def playbook_of(skill: Optional[Mapping[str, Any]]) -> Optional[Any]:
    """The raw playbook section of a stored skill, or None for a skill that is not one."""
    metadata = (skill or {}).get("Metadata") or {}
    if not isinstance(metadata, Mapping):
        return None
    return metadata.get(PLAYBOOK_KEY)


def is_playbook(skill: Optional[Mapping[str, Any]]) -> bool:
    return isinstance(playbook_of(skill), Mapping)


# ── suggestion ───────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: Any) -> List[str]:
    """The words of ``text``, lowercase, without numbers ("Sprint 42 planning" is "sprint planning")."""
    return [w for w in _WORD.findall(str(text or "").lower()) if not w.isdigit()]


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(
        list(haystack[i : i + width]) == list(needle) for i in range(len(haystack) - width + 1)
    )


def suggest(
    candidates: Iterable[Mapping[str, Any]],
    title: Any = "",
    agenda: Any = "",
    invitation: Any = "",
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """The playbooks a meeting's title, agenda or invitation suggest, best first.

    Each candidate is a stored skill. A phrase from its ``suggestFor``, or its
    display name, found as whole words in the title scores more than one found
    only in the agenda or the invitation, and a longer phrase more than a shorter
    one. Nothing is suggested without a match: a suggestion is shown to the
    person choosing, never applied by itself.
    """
    title_words = _words(title)
    other_words = _words(f"{agenda or ''} {invitation or ''}")
    found: List[Dict[str, Any]] = []
    for skill in candidates:
        playbook, _ = lenient_playbook(playbook_of(skill))
        if not playbook:
            continue
        phrases = list(playbook.get("suggestFor") or [])
        display = str(skill.get("DisplayName") or "").strip()
        if display and display not in phrases:
            phrases.append(display)
        best: Optional[Tuple[int, str, str]] = None
        for phrase in phrases:
            words = _words(phrase)
            if _contains(title_words, words):
                hit = (10 + len(words), phrase, "title")
            elif _contains(other_words, words):
                hit = (2 + len(words), phrase, "agenda")
            else:
                continue
            if best is None or hit[0] > best[0]:
                best = hit
        if best is None:
            continue
        score, phrase, where = best
        found.append(
            {
                "SkillId": str(skill.get("SkillId") or ""),
                "Name": str(skill.get("Name") or ""),
                "DisplayName": display or str(skill.get("Name") or ""),
                "Score": score,
                "MatchedPhrase": phrase,
                "MatchedIn": where,
                "Reason": (
                    f"The meeting's title mentions “{phrase}”."
                    if where == "title"
                    else f"The meeting's agenda or invitation mentions “{phrase}”."
                ),
            }
        )
    found.sort(key=lambda s: (-s["Score"], s["DisplayName"].lower()))
    return found[:limit]


# ── what a meeting runs with ─────────────────────────────────────────────────


def meeting_guidance(meeting: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The guidance part of the playbook applied to a meeting, or None."""
    playbook = (meeting or {}).get("Playbook")
    if not isinstance(playbook, Mapping) or not playbook.get("SkillId"):
        return None
    guidance = playbook.get("Guidance")
    return guidance if isinstance(guidance, Mapping) else None


def effective_guidance_settings(
    settings: Mapping[str, Any], meeting: Optional[Mapping[str, Any]]
) -> Tuple[Dict[str, Any], Optional[List[str]]]:
    """The live guidance settings a meeting runs with, and the types it may publish.

    Without a playbook (or one with no guidance part), the deployment's settings
    and None: every type, as before. With one, only the checks it names; its
    assertiveness's confidence threshold in place of the deployment's; its item rate but never
    more than the deployment's; and a research check only where the
    deployment's own switch for it is on. The digest, made on request, is
    always allowed.
    """
    out = dict(settings)
    guidance = meeting_guidance(meeting)
    if guidance is None:
        return out, None
    checks = [c for c in guidance.get("Checks") or [] if c in GUIDANCE_CHECKS]
    for check, switch in RESEARCH_CHECKS.items():
        if check not in checks:
            out[switch] = False
        elif out.get(switch) is not True:
            checks.remove(check)
            out[switch] = False
    threshold = ASSERTIVENESS.get(str(guidance.get("Assertiveness") or ""))
    if threshold is not None:
        out["confidenceThreshold"] = threshold
    items = _number(guidance.get("MaxItemsPer10Min"))
    deployment_items = _number(settings.get("maxItemsPer10Min"))
    if items is not None and items >= 0:
        out["maxItemsPer10Min"] = int(
            min(items, deployment_items) if deployment_items is not None else items
        )
    return out, checks + list(ALWAYS_ALLOWED_TYPES)


# ── cost ─────────────────────────────────────────────────────────────────────

#: The tokens of one run, as docs/live-guidance.md and docs/meeting-whiteboards.md
#: give them: measured for live guidance and its research, estimated for the
#: whiteboards.
RUN_TOKENS: Dict[str, Dict[str, float]] = {
    "guidance": {
        "inputTokens": 550,
        "cacheReadInputTokens": 1800,
        "cacheWriteInputTokens": 800,
        "outputTokens": 1400,
    },
    "whiteboards": {"inputTokens": 4000, "cacheReadInputTokens": 2000, "outputTokens": 1500},
    "research": {
        "cacheReadInputTokens": 7000,
        "cacheWriteInputTokens": 3500,
        "outputTokens": 1000,
    },
}


def _int_setting(settings: Mapping[str, Any], key: str, default: int) -> int:
    value = _number(settings.get(key))
    return int(value) if value is not None else default


def _run_price(price: Optional[Mapping[str, Any]], tokens: Mapping[str, float]) -> Optional[float]:
    if not isinstance(price, Mapping):
        return None
    total = 0.0
    for unit, count in tokens.items():
        unit_price = _number(price.get(unit))
        if unit_price is None:
            # A model priced without a cache price: its cached tokens as input.
            unit_price = _number(price.get("inputTokens"))
        if unit_price is None:
            return None
        total += unit_price * count
    return total


def estimate_cost(
    playbook: Mapping[str, Any],
    guidance_settings: Mapping[str, Any],
    whiteboard_settings: Mapping[str, Any],
    price_of: Callable[[str], Optional[Mapping[str, Any]]],
    *,
    guidance_model: str,
    whiteboard_model: str,
) -> Dict[str, Any]:
    """What the model calls a playbook turns on may cost a meeting, at most.

    Per meeting-hour for a meeting that talks enough for every run to happen,
    and per meeting at the deployment's caps. A model the price table does not
    list has no figure, and the result says so rather than guessing.
    """
    lines: List[Dict[str, Any]] = []
    guidance = playbook.get("guidance") or {}
    checks = [c for c in guidance.get("checks") or [] if c in GUIDANCE_CHECKS]
    if checks:
        interval = max(60, _int_setting(guidance_settings, "intervalSeconds", 90))
        cap = _int_setting(guidance_settings, "maxRunsPerMeeting", 120)
        lines.append(
            _line("Live guidance", "guidance", 3600 // interval, cap, price_of(guidance_model))
        )
        research = [
            c
            for c in checks
            if c in RESEARCH_CHECKS and guidance_settings.get(RESEARCH_CHECKS[c]) is True
        ]
        if research:
            research_cap = min(
                _int_setting(guidance_settings, "researchRunsPerMeeting", 20),
                _int_setting(guidance_settings, "researchChecksPerMeeting", 30),
            )
            lines.append(
                _line(
                    "Live guidance fact checks and lookups",
                    "research",
                    min(3600 // interval, research_cap),
                    research_cap,
                    price_of(guidance_model),
                )
            )
    if any(b.get("assistantUpdates") for b in playbook.get("whiteboards") or []):
        interval = max(60, _int_setting(whiteboard_settings, "intervalSeconds", 180))
        cap = _int_setting(whiteboard_settings, "maxRunsPerMeeting", 60)
        lines.append(
            _line(
                "Meeting whiteboards",
                "whiteboards",
                3600 // interval,
                cap,
                price_of(whiteboard_model),
            )
        )
    priced = [line for line in lines if line["perRun"] is not None]
    unpriced = [line["what"] for line in lines if line["perRun"] is None]
    return {
        "currency": "USD",
        "lines": lines,
        "perHourAtMost": round(sum(line["perHour"] for line in priced), 4) if lines else 0.0,
        "perMeetingAtMost": round(sum(line["perMeeting"] for line in priced), 4) if lines else 0.0,
        "unpriced": unpriced,
        "basis": (
            "An estimate from the shipped price table and the token use of a typical run, "
            "for a meeting busy enough for every run to happen; a quiet meeting runs less "
            "often. The meeting's own cost estimate shows what it used."
        ),
    }


def _line(
    what: str, kind: str, per_hour: int, cap: int, price: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    per_run = _run_price(price, RUN_TOKENS[kind])
    per_hour = max(0, min(per_hour, cap))
    return {
        "what": what,
        "runsPerHourAtMost": per_hour,
        "runsPerMeetingAtMost": max(0, cap),
        "perRun": round(per_run, 6) if per_run is not None else None,
        "perHour": round(per_run * per_hour, 4) if per_run is not None else None,
        "perMeeting": round(per_run * max(0, cap), 4) if per_run is not None else None,
    }


# ── the meeting's record of it ───────────────────────────────────────────────


def quick_actions_of(meeting: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    playbook = (meeting or {}).get("Playbook")
    if not isinstance(playbook, Mapping):
        return []
    return [
        {"Label": str(a.get("Label")), "Prompt": str(a.get("Prompt"))}
        for a in playbook.get("QuickActions") or []
        if isinstance(a, Mapping) and a.get("Label") and a.get("Prompt")
    ]
