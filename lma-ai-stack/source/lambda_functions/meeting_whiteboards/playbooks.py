#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting playbooks: applying one to a meeting, and what the pages offer to choose from.

A playbook is a skill with a ``playbook`` section (lma_playbooks.rules). This
module answers two AppSync fields, through the meeting whiteboards resolver
(the function that already makes whiteboards and changes the meeting's
switches):

``meetingPlaybookOptions``
    Every playbook, each with what applying it turns on and an estimate of what
    that may cost; those a title, agenda or invitation suggest; the profile's
    default; and the meeting's own playbook when a CallId is given. Read-only,
    for every signed-in user who is not a Participant.
``applyMeetingPlaybook``
    Apply a playbook to a meeting, switch to another, or remove it. For the
    meeting's owner, Admins and LMA's own backend (the call event processor,
    for a Virtual Participant's choice and the profile's default). Applying
    creates the playbook's whiteboards, turns on live guidance and the
    whiteboard updates when it names checks and kept whiteboards, and records
    on the meeting the checks, the quick actions and what was on before it.
    Removing it puts that back; the whiteboards it made are kept unless
    ``DeleteWhiteboards`` is set, and then only those only the assistant has written on.

A playbook applies what it can: a part a release does not know (a template or
a check) is left out and noted on the meeting rather than refusing the rest.
See docs/meeting-playbooks.md.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional

import index as wb
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_config import (
    CALL_PRICING,
    LANGUAGE_MODELS,
    MEETING_ASSIST,
    ConfigurationStore,
    read_setting,
    resolve_model_id,
)
from lma_metering import bedrock_service
from lma_metering.pricing import prices_for_region
from lma_notifications import NotificationError, appsync_graphql
from lma_playbooks import rules as playbook_rules
from lma_whiteboards import rules as board_rules

# pylint: enable=import-error

LOGGER = logging.getLogger()

SKILLS_TABLE = os.environ.get("SKILLS_TABLE", "")
SKILLS_ACCOUNT_ID = os.environ.get("SKILLS_ACCOUNT_ID", "")
PARTICIPANT_GROUP = os.environ.get("PARTICIPANT_GROUP", "Participant")
DEPLOYMENT_REGION = os.environ.get("AWS_REGION", "us-east-1")

#: Why a playbook was applied, as the meeting records it.
ORIGIN_CHOSEN = "chosen"
ORIGIN_VIRTUAL_PARTICIPANT = "virtual-participant"
ORIGIN_PROFILE_DEFAULT = "profile-default"
BACKEND_ORIGINS = (ORIGIN_VIRTUAL_PARTICIPANT, ORIGIN_PROFILE_DEFAULT)
#: The value a Virtual Participant stores for "no playbook", whatever the profile's default.
NO_PLAYBOOK = "none"
#: A skill id, as the Skills Manager makes one.
SKILL_ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
#: Who a whiteboard a playbook makes is recorded as made by.
BOARD_AUTHOR_PREFIX = "Playbook"

_SKILL_FIELDS = (
    "SkillId",
    "Name",
    "DisplayName",
    "Description",
    "Metadata",
    "SourceFormat",
    "UpdatedAt",
)

_STORE: Optional[ConfigurationStore] = None


def _store() -> ConfigurationStore:
    global _STORE  # pylint: disable=global-statement
    if _STORE is None:
        _STORE = ConfigurationStore()
    return _STORE


# ── the skills ───────────────────────────────────────────────────────────────


def _skill_query(**extra: Any) -> List[Dict[str, Any]]:
    names = {f"#a{i}": n for i, n in enumerate(_SKILL_FIELDS)}
    kwargs: Dict[str, Any] = {
        "TableName": SKILLS_TABLE,
        "KeyConditionExpression": "AccountId = :account",
        "ExpressionAttributeValues": {":account": {"S": SKILLS_ACCOUNT_ID}},
        "ProjectionExpression": ", ".join(names),
        "ExpressionAttributeNames": names,
        **extra,
    }
    items: List[Dict[str, Any]] = []
    while True:
        response = wb.DYNAMODB.query(**kwargs)
        items.extend(row for row in (wb._from_item(i) for i in response.get("Items", [])) if row)
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def stored_playbooks() -> List[Dict[str, Any]]:
    """Every skill that is a playbook, by display name."""
    if not SKILLS_TABLE:
        return []
    found = [s for s in _skill_query() if playbook_rules.is_playbook(s)]
    return sorted(found, key=lambda s: str(s.get("DisplayName") or s.get("Name") or "").lower())


def stored_skill(skill_id: str) -> Optional[Dict[str, Any]]:
    if not SKILLS_TABLE or not skill_id:
        return None
    names = {f"#a{i}": n for i, n in enumerate(_SKILL_FIELDS)}
    response = wb.DYNAMODB.get_item(
        TableName=SKILLS_TABLE,
        Key={"AccountId": {"S": SKILLS_ACCOUNT_ID}, "SkillId": {"S": skill_id}},
        ProjectionExpression=", ".join(names),
        ExpressionAttributeNames=names,
    )
    return wb._from_item(response.get("Item"))


# ── the deployment ───────────────────────────────────────────────────────────


def guidance_settings() -> Dict[str, Any]:
    try:
        raw = read_setting(MEETING_ASSIST.name, "guidance", default={})
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("Could not read the Live guidance settings; using the shipped ones")
        raw = {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def whiteboard_settings_raw() -> Dict[str, Any]:
    try:
        raw = read_setting(MEETING_ASSIST.name, "whiteboards", default={})
    except Exception:  # pylint: disable=broad-except
        raw = {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def default_playbook() -> str:
    """The configuration profile's default playbook (its skill id), or ''."""
    try:
        raw = read_setting(MEETING_ASSIST.name, "playbooks", default={})
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("Could not read the Playbooks settings; no default playbook")
        return ""
    value = (raw or {}).get("defaultPlaybook") if isinstance(raw, Mapping) else ""
    # The call event processor's rule (default_playbook there): a skill id or nothing.
    value = value.strip() if isinstance(value, str) else ""
    return value if SKILL_ID.fullmatch(value) else ""


def _model(setting: Any) -> str:
    """The model a setting names (else the Meeting assistant model), as the runs call it."""
    try:
        if isinstance(setting, str) and setting:
            return resolve_model_id(setting, region=wb.BEDROCK_REGION)
        return resolve_model_id(
            read_setting(
                LANGUAGE_MODELS.name, "meetingAssistModelId", default=wb.FALLBACK_MODEL_ID
            ),
            region=wb.BEDROCK_REGION,
        )
    except Exception:  # pylint: disable=broad-except
        return wb.FALLBACK_MODEL_ID


def _price_lookup():
    try:
        section = _store().get_section(CALL_PRICING.name)
        prices, _region_listed = prices_for_region(section.values or {}, DEPLOYMENT_REGION)
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("Could not read the price table; playbook estimates are unpriced")
        prices = {}

    def price_of(model_id: str) -> Optional[Mapping[str, Any]]:
        entry = prices.get(bedrock_service(model_id))
        if entry is None and "." in model_id:
            # A geography prefix the table does not list: the same model's base id.
            for prefix in ("global.", "us.", "eu.", "apac.", "jp.", "au.", "us-gov."):
                if model_id.startswith(prefix):
                    entry = prices.get(bedrock_service("global." + model_id[len(prefix) :]))
                    break
        return entry if isinstance(entry, Mapping) else None

    return price_of


def deployment_limits(guidance: Mapping[str, Any], boards: board_rules.Settings) -> Dict[str, Any]:
    """What the deployment allows a playbook to turn on."""
    runs = guidance.get("maxRunsPerMeeting", 120)
    return {
        "guidanceAvailable": not (isinstance(runs, (int, float)) and runs <= 0),
        "whiteboardUpdatesAvailable": boards.assistant_available,
        "factChecksEnabled": guidance.get("factChecksEnabled") is True,
        "lookupsEnabled": guidance.get("lookupsEnabled") is True,
        "maxWhiteboards": boards.max_whiteboards,
        "maxItemsPer10Min": guidance.get("maxItemsPer10Min", 10),
    }


def describe(
    skill: Mapping[str, Any],
    limits: Mapping[str, Any],
    estimate: Any,
) -> Dict[str, Any]:
    """A playbook as the pages show it: what applying it turns on, and its cost."""
    playbook, notes = playbook_rules.lenient_playbook(playbook_rules.playbook_of(skill))
    guidance = playbook.get("guidance") or {}
    checks = []
    for check in guidance.get("checks") or []:
        switch = playbook_rules.RESEARCH_CHECKS.get(check)
        available = limits["guidanceAvailable"] and (
            switch is None
            or limits.get("factChecksEnabled" if check == "fact_check" else "lookupsEnabled")
        )
        checks.append(
            {
                "check": check,
                "label": playbook_rules.GUIDANCE_CHECKS[check],
                "available": bool(available),
            }
        )
    return {
        "SkillId": skill.get("SkillId"),
        "Name": skill.get("Name"),
        "DisplayName": skill.get("DisplayName") or skill.get("Name"),
        "Description": skill.get("Description") or "",
        "Shipped": skill.get("SourceFormat") == "shipped",
        "Whiteboards": [
            {
                "template": board["template"],
                "templateLabel": board_rules.TEMPLATES[board["template"]]["label"],
                "title": board["title"],
                "instructions": board["instructions"],
                "assistantUpdates": board["assistantUpdates"],
            }
            for board in playbook.get("whiteboards") or []
        ],
        "Guidance": (
            {
                "checks": checks,
                "assertiveness": guidance.get("assertiveness"),
                "maxItemsPer10Min": guidance.get("maxItemsPer10Min"),
            }
            if guidance
            else None
        ),
        "QuickActions": playbook.get("quickActions") or [],
        "SuggestFor": playbook.get("suggestFor") or [],
        "Notes": notes,
        "Estimate": estimate(playbook),
    }


def options(caller: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """``meetingPlaybookOptions``."""
    current = None
    title = arguments.get("Title") or ""
    agenda = arguments.get("Agenda") or ""
    call_id = arguments.get("CallId")
    if call_id:
        meeting = wb.read_meeting(str(call_id), full=True)
        if meeting is None or not wb.may_see(caller, meeting):
            raise wb.refuse("Unauthorized", "Unauthorized: you may not see this meeting")
        current = meeting.get("Playbook")
        # A meeting's CallId is its name (a Virtual Participant's meeting name
        # with the time added); the suggestion ignores the numbers in it.
        title = title or meeting.get("CallId") or ""
        agenda = agenda or meeting.get("Agenda") or ""
    guidance = guidance_settings()
    boards_raw = whiteboard_settings_raw()
    boards = board_rules.Settings.from_raw(boards_raw)
    limits = deployment_limits(guidance, boards)
    price_of = _price_lookup()
    guidance_model = _model(guidance.get("modelId"))
    board_model = _model(boards.model_id)

    def estimate(playbook: Mapping[str, Any]) -> Dict[str, Any]:
        return playbook_rules.estimate_cost(
            playbook,
            guidance,
            boards_raw,
            price_of,
            guidance_model=guidance_model,
            whiteboard_model=board_model,
        )

    stored = stored_playbooks()
    return {
        "Playbooks": [describe(skill, limits, estimate) for skill in stored],
        "Suggestions": playbook_rules.suggest(
            stored, title, agenda, arguments.get("Invitation") or ""
        ),
        "ProfileDefault": default_playbook() or None,
        "Current": current,
        "Deployment": limits,
        "Templates": board_rules.template_listing(),
        "Checks": [{"check": k, "label": v} for k, v in playbook_rules.GUIDANCE_CHECKS.items()],
    }


# ── applying one ─────────────────────────────────────────────────────────────

_DELETE_MUTATION = (
    "mutation DeleteMeetingWhiteboard($input: DeleteMeetingWhiteboardInput!) {\n"
    "  deleteMeetingWhiteboard(input: $input) { CallId WhiteboardId Deleted Owner SharedWith }\n}"
)


def _may_apply(caller: Mapping[str, Any], meeting: Mapping[str, Any]) -> bool:
    """The meeting's owner, an Admin, or LMA's own backend."""
    if caller["is_backend"] or caller["is_admin"]:
        return True
    return bool(caller["username"]) and meeting.get("Owner") == caller["username"]


def _untouched(board: Mapping[str, Any]) -> bool:
    """Nobody has written on it: every panel is still the assistant's."""
    panels = board.get("Panels") or []
    return all(p.get("Source") == board_rules.SOURCE_ASSISTANT for p in panels)


def _remove_boards(call_id: str, record: Mapping[str, Any], notes: List[str]) -> None:
    # Only those the playbook made: one it found on the meeting and kept is not its own.
    made = {
        str(b.get("WhiteboardId")) for b in record.get("Whiteboards") or [] if not b.get("Reused")
    }
    if not made:
        return
    kept = 0
    for board in wb.read_boards(call_id):
        if str(board.get("WhiteboardId")) not in made:
            continue
        if not _untouched(board):
            kept += 1
            continue
        try:
            appsync_graphql(
                _DELETE_MUTATION,
                {"input": {"CallId": call_id, "WhiteboardId": board["WhiteboardId"]}},
            )
        except NotificationError as error:
            LOGGER.warning("Could not delete whiteboard %s: %s", board["WhiteboardId"], error)
            kept += 1
    if kept:
        notes.append(
            f"Kept {kept} whiteboard{'s' if kept != 1 else ''} the previous playbook made, "
            "because someone has written on it."
        )


def _create_boards(
    call_id: str,
    playbook: Mapping[str, Any],
    display_name: str,
    settings: board_rules.Settings,
    notes: List[str],
    own: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """The playbook's whiteboards, made or found: one of the same title and template is reused."""
    existing = wb.read_boards(call_id)
    count = len(existing)
    made: List[Dict[str, Any]] = []
    for board in playbook.get("whiteboards") or []:
        same = next(
            (
                b
                for b in existing
                if b.get("Title") == board["title"] and b.get("Template") == board["template"]
            ),
            None,
        )
        if same is not None:
            entry = {
                "WhiteboardId": same["WhiteboardId"],
                "Title": board["title"],
                "Template": board["template"],
            }
            # One this playbook made on an earlier apply stays its own.
            if str(same["WhiteboardId"]) not in (own or set()):
                entry["Reused"] = True
            made.append(entry)
            continue
        if count >= settings.max_whiteboards:
            notes.append(
                f"The whiteboard “{board['title']}” was not made: the meeting already has "
                f"{count} whiteboards, the most this deployment allows."
            )
            continue
        try:
            data = appsync_graphql(
                wb.CREATE_MUTATION,
                {
                    "input": {
                        "CallId": call_id,
                        "Title": board["title"],
                        "Template": board["template"],
                        "Instructions": board["instructions"],
                        "AssistantUpdates": board["assistantUpdates"],
                        "Source": board_rules.SOURCE_ASSISTANT,
                        "UpdatedBy": board_rules.attribution(
                            f"{BOARD_AUTHOR_PREFIX}: {display_name}", board_rules.ASSISTANT_LABEL
                        ),
                    }
                },
            )
        except NotificationError as error:
            LOGGER.warning("Playbook whiteboard %s not made: %s", board["title"], error)
            notes.append(f"The whiteboard “{board['title']}” could not be made just now.")
            continue
        created = data.get("createMeetingWhiteboard") or {}
        count += 1
        made.append(
            {
                "WhiteboardId": created.get("WhiteboardId"),
                "Title": board["title"],
                "Template": board["template"],
            }
        )
    return made


def _record(
    skill: Mapping[str, Any],
    playbook: Mapping[str, Any],
    *,
    origin: str,
    actor: str,
    boards: List[Dict[str, Any]],
    notes: List[str],
    previous: Mapping[str, Any],
) -> Dict[str, Any]:
    guidance = playbook.get("guidance")
    if guidance and not guidance.get("checks"):
        # Every check it names was left out: no guidance part, so live guidance is left as it was.
        guidance = None
    return {
        "SkillId": skill.get("SkillId"),
        "Name": skill.get("Name"),
        "DisplayName": skill.get("DisplayName") or skill.get("Name"),
        "AppliedAt": wb.now_iso(),
        "AppliedBy": actor,
        "Origin": origin,
        "Guidance": (
            {
                "Checks": list(guidance.get("checks") or []),
                "Assertiveness": guidance.get("assertiveness"),
                **(
                    {"MaxItemsPer10Min": guidance["maxItemsPer10Min"]}
                    if guidance.get("maxItemsPer10Min") is not None
                    else {}
                ),
            }
            if guidance
            else None
        ),
        "QuickActions": [
            {"Label": a["label"], "Prompt": a["prompt"]} for a in playbook.get("quickActions") or []
        ],
        "Whiteboards": boards,
        "Notes": notes,
        "Previous": dict(previous),
    }


def _write(
    caller: Mapping[str, Any],
    call_id: str,
    meeting: Mapping[str, Any],
    record: Optional[Dict[str, Any]],
    switches: Mapping[str, Optional[bool]],
) -> Dict[str, Any]:
    """Store the meeting's playbook and switches, as one write conditional on the access read."""
    _meeting, check = wb.load_for_write(caller, call_id)
    condition = check["ConditionCheck"]
    names = {**condition["ExpressionAttributeNames"], "#p": "Playbook", "#pu": "PlaybookUpdatedAt"}
    values = {**condition.get("ExpressionAttributeValues", {}), ":pu": {"S": wb.now_iso()}}
    sets = ["#pu = :pu"]
    removes: List[str] = []
    if record is None:
        removes.append("#p")
    else:
        sets.append("#p = :p")
        values[":p"] = wb.SERIALIZER.serialize(wb._dynamo(_without_none(record)))
    for index, (name, value) in enumerate(sorted(switches.items())):
        if value is None:
            continue
        names[f"#s{index}"] = name
        values[f":s{index}"] = {"BOOL": value}
        sets.append(f"#s{index} = :s{index}")
    expression = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    del meeting
    try:
        result = wb.DYNAMODB.update_item(
            TableName=wb.EVENT_SOURCING_TABLE,
            Key=wb._meeting_key(call_id),
            UpdateExpression=expression,
            ConditionExpression=condition["ConditionExpression"],
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if wb._conflict(error):
            raise wb._meeting_refusal(caller, call_id) from error
        raise
    return wb._from_item(result["Attributes"]) or {}


def _without_none(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_none(v) for v in value]
    return value


def apply(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    """``applyMeetingPlaybook``: the meeting row afterwards."""
    call_id = wb.required_call_id(data)
    meeting = wb.read_meeting(call_id, full=True)
    if meeting is None or not wb.may_see(caller, meeting):
        raise wb.refuse("Unauthorized", "Unauthorized: you may not change this meeting")
    if not _may_apply(caller, meeting):
        raise wb.refuse(
            "Unauthorized", "Only the meeting's owner or an Admin may choose its playbook"
        )
    if meeting.get("TranscriptDeletedAt"):
        raise wb.refuse("WhiteboardTranscriptDeleted", "The meeting's transcript is deleted")
    skill_id = str(data.get("SkillId") or "").strip()
    if skill_id == NO_PLAYBOOK:
        skill_id = ""
    origin = ORIGIN_CHOSEN
    actor = caller["username"]
    if caller["is_backend"]:
        origin = str(data.get("Origin")) if data.get("Origin") in BACKEND_ORIGINS else ORIGIN_CHOSEN
        actor = board_rules.attribution(data.get("UpdatedBy"), "LMA")
    if skill_id and meeting.get("Status") == "ENDED":
        raise wb.refuse(
            "BadRequest", "The meeting has ended; a playbook applies to a meeting in progress"
        )

    current = meeting.get("Playbook") if isinstance(meeting.get("Playbook"), Mapping) else None
    applied = current if current and current.get("SkillId") else None
    # What was on before any playbook: kept across a switch, so removing the last
    # one puts the meeting back as it was before the first.
    previous = (applied or {}).get("Previous") or {
        "GuidanceEnabled": meeting.get("GuidanceEnabled") is True,
        "WhiteboardsEnabled": meeting.get("WhiteboardsEnabled") is True,
    }
    notes: List[str] = []
    skill = stored_skill(skill_id) if skill_id else None
    target_missing = bool(skill_id) and (skill is None or not playbook_rules.is_playbook(skill))
    if target_missing and not caller["is_backend"]:
        # Refused before anything changes, the current playbook's whiteboards included.
        raise wb.refuse("BadRequest", f"There is no playbook {skill_id}")
    if applied and data.get("DeleteWhiteboards") is True:
        _remove_boards(call_id, applied, notes)

    if not skill_id:
        LOGGER.info("Playbook removed from %s by %s", call_id, actor)
        return _write(
            caller,
            call_id,
            meeting,
            None,
            {
                "GuidanceEnabled": previous.get("GuidanceEnabled") is True,
                "WhiteboardsEnabled": previous.get("WhiteboardsEnabled") is True,
            },
        )

    if target_missing or skill is None:
        # A Virtual Participant scheduled with a playbook deleted since, or a
        # profile default that no longer exists: the meeting says so and runs
        # without one.
        LOGGER.warning("Playbook %s for %s does not exist; none applied", skill_id, call_id)
        record = {
            "Requested": skill_id,
            "Origin": origin,
            "AppliedAt": wb.now_iso(),
            "Notes": [
                f"The playbook “{skill_id}” chosen for this meeting no longer exists, so no "
                "playbook was applied."
            ],
        }
        return _write(caller, call_id, meeting, record, {})

    playbook, problems = playbook_rules.lenient_playbook(playbook_rules.playbook_of(skill))
    notes.extend(f"Left out: {problem}." for problem in problems)
    display_name = str(skill.get("DisplayName") or skill.get("Name") or skill_id)
    board_settings = board_rules.Settings.from_raw(whiteboard_settings_raw())
    guidance = guidance_settings()
    limits = deployment_limits(guidance, board_settings)
    own = {
        str(b.get("WhiteboardId"))
        for b in ((applied or {}).get("Whiteboards") or [])
        if not b.get("Reused") and (applied or {}).get("SkillId") == skill_id
    }
    boards = _create_boards(call_id, playbook, display_name, board_settings, notes, own)

    switches: Dict[str, Optional[bool]] = {
        # Off unless something below turns it on: a switch the previous playbook
        # turned on goes back to what it was before.
        "GuidanceEnabled": previous.get("GuidanceEnabled") is True,
        "WhiteboardsEnabled": previous.get("WhiteboardsEnabled") is True,
    }
    checks = (playbook.get("guidance") or {}).get("checks") or []
    if checks:
        running = [
            c
            for c in checks
            if c not in playbook_rules.RESEARCH_CHECKS or limits[playbook_rules.RESEARCH_CHECKS[c]]
        ]
        for check, flag in playbook_rules.RESEARCH_CHECKS.items():
            if check in checks and not limits[flag]:
                notes.append(
                    f"{playbook_rules.GUIDANCE_CHECKS[check]} are turned off for this "
                    "deployment, so they do not run."
                )
        if not limits["guidanceAvailable"]:
            notes.append("Live guidance is turned off for this deployment, so no check runs.")
        elif running:
            switches["GuidanceEnabled"] = True
        else:
            # Every check it names is off here: live guidance would only bill runs.
            notes.append(
                "None of its checks can run in this deployment, so live guidance is left as it was."
            )
    if any(b.get("assistantUpdates") for b in playbook.get("whiteboards") or []):
        if limits["whiteboardUpdatesAvailable"]:
            switches["WhiteboardsEnabled"] = True
        else:
            notes.append(
                "The assistant's whiteboard updates are turned off for this deployment; the "
                "whiteboards are kept by hand."
            )
    record = _record(
        skill,
        playbook,
        origin=origin,
        actor=actor,
        boards=boards,
        notes=notes,
        previous=previous,
    )
    LOGGER.info(
        "Playbook %s applied to %s by %s (%s): whiteboards=%d checks=%s notes=%d",
        skill_id,
        call_id,
        actor,
        origin,
        len(boards),
        ",".join(checks) or "-",
        len(notes),
    )
    return _write(caller, call_id, meeting, record, switches)


# ── at a meeting's start ─────────────────────────────────────────────────────

_APPLY_MUTATION_HEAD = (
    "mutation ApplyMeetingPlaybook($input: ApplyMeetingPlaybookInput!) {\n"
    "  applyMeetingPlaybook(input: $input) {"
)


def apply_at_start(event: Mapping[str, Any]) -> Dict[str, Any]:
    """An asynchronous invoke from the call event processor for a new meeting.

    ``{"PlaybookApply": {"CallId", "SkillId", "Origin", "UpdatedBy"}}``. Goes
    through AppSync as the backend, so the meeting's open pages receive the
    change through onUpdateCall like any other.
    """
    request = event.get("PlaybookApply") or {}
    call_id = request.get("CallId")
    skill_id = str(request.get("SkillId") or "").strip()
    if not isinstance(call_id, str) or not call_id or not skill_id or skill_id == NO_PLAYBOOK:
        return {"applied": False, "reason": "nothing to apply"}
    origin = (
        request.get("Origin")
        if request.get("Origin") in BACKEND_ORIGINS
        else ORIGIN_PROFILE_DEFAULT
    )
    meeting = wb.read_meeting(call_id, full=True)
    if meeting is None:
        return {"applied": False, "reason": "no such meeting"}
    if isinstance(meeting.get("Playbook"), Mapping):
        # Chosen on the meeting page before this ran: the person's choice stands.
        return {"applied": False, "reason": "the meeting already has a playbook"}
    try:
        appsync_graphql(
            _APPLY_MUTATION_HEAD + wb.SUBSCRIBED_CALL_FIELDS + "}\n}",
            {
                "input": {
                    "CallId": call_id,
                    "SkillId": skill_id,
                    "Origin": origin,
                    "UpdatedBy": str(request.get("UpdatedBy") or "LMA"),
                }
            },
        )
    except NotificationError as error:
        LOGGER.warning("Playbook %s not applied to %s: %s", skill_id, call_id, error)
        return {"applied": False, "reason": str(error)}
    return {"applied": True}


QUERIES = {"meetingPlaybookOptions": options}
MUTATIONS = {"applyMeetingPlaybook": apply}


def is_participant(event: Mapping[str, Any]) -> bool:
    """A signed-in Participant: refused, as the schema's directives refuse them."""
    claims = (event.get("identity") or {}).get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",")]
    return PARTICIPANT_GROUP in groups and wb.ADMIN_GROUP not in groups
