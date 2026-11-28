# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting playbooks on a live stack (docs/meeting-playbooks.md).

Synthetic meetings (START events on the CallDataStream, as
meeting_whiteboards_probe sends them) owned by a throwaway User, with a second
User and a Participant made beside it (role_access_probe), all deleted
afterwards:

* the shipped playbooks are seeded as shipped skills with their playbook
  sections;
* a meeting named for sprint planning is suggested the Sprint planning playbook,
  and a weekly sync is suggested none;
* the owner applies it: its three whiteboards exist with their templates, live
  guidance and the whiteboard updates are on, its checks are recorded and no
  other, and its quick actions are listed; the second User (not the owner) and
  the Participant are refused, the Participant the options too;
* switching to Retrospective with DeleteWhiteboards removes the Sprint planning
  whiteboards nobody wrote on; removing it puts back what was on before;
* a START that names a playbook, as a Virtual Participant's does, gets it
  applied once the meeting exists; one that names a playbook that no longer
  exists runs without one and says why;
* a Virtual Participant scheduled a day ahead stores its playbook, and an edit
  before it starts changes it (cancelled afterwards).

Opt-in (``LMA_TEST_PLAYBOOK_DEFAULT=1``, about six minutes, changes the active
profile's Meeting playbooks setting for the run and restores it): with the
profile's default playbook set, a new meeting that names none gets it.

No model call is made: nothing is said in the meetings. Every meeting is removed
by its CallId at the end.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import pytest

SHIPPED_PLAYBOOKS = {
    "architecture-review": ["architecture", "decision_log", "decisions"],
    "customer-discovery": ["blank", "comparison"],
    "incident-review": ["timeline", "decisions", "blank"],
    "retrospective": ["retrospective"],
    "sprint-planning": ["user_stories", "gantt", "risks"],
}
SPRINT_CHECKS = [
    "action_missing_owner",
    "action_missing_due_date",
    "clarify",
    "risk_probe",
    "uncovered_topic",
    "time_check",
]
APPLY_WAIT_SECONDS = 90
DEFAULT_WAIT_SECONDS = 420

PLAYBOOK_FIELDS = """
  CallId GuidanceEnabled WhiteboardsEnabled PlaybookUpdatedAt
  Playbook { SkillId Name DisplayName Requested Origin AppliedBy Notes
    Guidance { Checks Assertiveness MaxItemsPer10Min }
    QuickActions { Label Prompt }
    Whiteboards { WhiteboardId Title Template Reused }
    Previous { GuidanceEnabled WhiteboardsEnabled } }
"""
APPLY = (
    "mutation A($i: ApplyMeetingPlaybookInput!) { applyMeetingPlaybook(input: $i) {"
    + PLAYBOOK_FIELDS
    + "} }"
)
OPTIONS = (
    "query O($c: ID, $t: String) { meetingPlaybookOptions(CallId: $c, Title: $t) }"
)
BOARDS = "query L($c: ID!) { listMeetingWhiteboards(CallId: $c) { Items { WhiteboardId Title Template } } }"
VP_FIELDS = "id status playbook"
CREATE_VP = (
    "mutation C($input: CreateVirtualParticipantInput!) { createVirtualParticipant(input: $input) { "
    + VP_FIELDS
    + " } }"
)
EDIT_VP = (
    "mutation E($input: UpdateScheduledVirtualParticipantInput!) { "
    "updateScheduledVirtualParticipant(input: $input) { " + VP_FIELDS + " } }"
)
GET_VP = "query G($id: ID!) { getVirtualParticipant(id: $id) { " + VP_FIELDS + " } }"


def _options(value) -> dict:
    return json.loads(value) if isinstance(value, str) else value


class World:
    """The deployment, three throwaway users, and the meetings the tests made."""

    def __init__(self, stack_name: str, region: str | None):
        import role_access_probe as rbac  # pylint: disable=import-outside-toplevel
        from meeting_whiteboards_probe import Deployment  # pylint: disable=import-outside-toplevel

        self.rbac = rbac
        self.roles = rbac.Deployment.find(stack_name, region)
        self.d = Deployment(stack_name, region)
        stamp = (
            datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]
        )
        self.users = rbac.create_role_users(
            self.roles,
            f"pb{stamp}",
            (("owner", "User"), ("other", "User"), ("participant", "Participant")),
        )
        self.owner, self.other, self.participant = self.users
        for user in self.users:
            rbac.sign_in(self.roles, user)
        # The owner as AppSync names a signed-in caller, which the meeting's Owner must equal.
        self.owner_name = (
            rbac.claims(self.owner.tokens["id"]).get("cognito:username")
            or self.owner.username
        )
        self.meetings: list = []

    def call(self, user, document: str, variables: dict) -> dict:
        return self.rbac.call(self.roles, user, document, variables)

    def data(self, user, document: str, variables: dict) -> dict:
        body = self.call(user, document, variables)
        assert not body.get("errors"), body.get("errors")
        return body["data"]

    def meeting(self, name: str, **start_fields):
        from meeting_whiteboards_probe import Meeting  # pylint: disable=import-outside-toplevel

        meeting = Meeting(self.d, self.owner_name, assisted=False)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        meeting.call_id = f"integ-test-playbooks {name} {stamp}-{uuid.uuid4().hex[:6]}"
        self.meetings.append(meeting)
        record = {
            "EventType": "START",
            "CallId": meeting.call_id,
            "CustomerPhoneNumber": "Integ Test Participant",
            "SystemPhoneNumber": "LMA Integ Test",
            "AgentId": self.owner_name,
            "CreatedAt": datetime.now(timezone.utc).isoformat(),
            "AccessToken": "",
            "IdToken": "",
            "RefreshToken": "",
            "WhiteboardsEnabled": False,
            "GuidanceEnabled": False,
            **start_fields,
        }
        meeting._put(record)  # pylint: disable=protected-access
        assert meeting.wait_created(), f"no meeting {meeting.call_id}"
        return meeting

    def boards(self, meeting) -> list[dict]:
        return self.data(self.owner, BOARDS, {"c": meeting.call_id})[
            "listMeetingWhiteboards"
        ]["Items"]

    def wait_playbook(self, meeting, seconds: int = APPLY_WAIT_SECONDS) -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            row = meeting.meeting() or {}
            if row.get("Playbook"):
                return row
            time.sleep(4)
        return meeting.meeting() or {}

    def close(self) -> None:
        for meeting in self.meetings:
            try:
                meeting.cleanup()
            except Exception as error:  # noqa: BLE001 - every meeting is tried
                print(f"cleanup of {meeting.call_id} failed: {error}")
        left = self.rbac.delete_role_users(self.roles, self.users)
        assert not left, f"users left behind: {left}"


@pytest.fixture(scope="module")
def world(client):
    made = World(client.stack_name, client.region)
    yield made
    made.close()


# ── seeding ──────────────────────────────────────────────────────────────────


def test_the_shipped_playbooks_are_seeded_as_shipped_skills(client):
    listed = {skill["Name"]: skill for skill in client.skills.list()}
    for name, templates in SHIPPED_PLAYBOOKS.items():
        assert name in listed, f"{name} is not seeded"
        assert listed[name]["SourceFormat"] == "shipped"
        metadata = listed[name]["Metadata"]
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
        assert [b["template"] for b in metadata["playbook"]["whiteboards"]] == templates


# ── suggestion and options ───────────────────────────────────────────────────


def test_a_matching_title_suggests_its_playbook_and_another_suggests_none(world):
    suggested = _options(
        world.data(world.owner, OPTIONS, {"t": "Sprint 42 planning - Orbit"})[
            "meetingPlaybookOptions"
        ]
    )
    assert suggested["Suggestions"][0]["SkillId"] == "sprint-planning"
    assert {p["SkillId"] for p in suggested["Playbooks"]} >= set(SHIPPED_PLAYBOOKS)
    sprint = next(
        p for p in suggested["Playbooks"] if p["SkillId"] == "sprint-planning"
    )
    assert sprint["Estimate"]["lines"], (
        "a playbook that turns guidance on has a cost estimate"
    )
    none = _options(
        world.data(world.owner, OPTIONS, {"t": "Weekly sync"})["meetingPlaybookOptions"]
    )
    assert none["Suggestions"] == []


def test_a_participant_is_refused_the_options_and_the_apply(world):
    meeting = world.meeting("participant-check")
    for document, variables in (
        (OPTIONS, {"t": "Sprint planning"}),
        (APPLY, {"i": {"CallId": meeting.call_id, "SkillId": "sprint-planning"}}),
    ):
        body = world.call(world.participant, document, variables)
        assert body.get("errors"), f"a Participant was answered: {body}"
    assert "Playbook" not in (meeting.meeting() or {})


# ── applying, switching, removing ────────────────────────────────────────────


@pytest.fixture(scope="module")
def sprint_meeting(world):
    meeting = world.meeting("sprint-42-planning")
    applied = world.data(
        world.owner,
        APPLY,
        {"i": {"CallId": meeting.call_id, "SkillId": "sprint-planning"}},
    )["applyMeetingPlaybook"]
    return meeting, applied


def test_applying_a_playbook_makes_its_whiteboards_with_their_templates(
    world, sprint_meeting
):
    meeting, applied = sprint_meeting
    templates = sorted(b["Template"] for b in world.boards(meeting))
    assert templates == sorted(SHIPPED_PLAYBOOKS["sprint-planning"])
    assert (
        sorted(b["Template"] for b in applied["Playbook"]["Whiteboards"]) == templates
    )


def test_applying_turns_on_only_the_checks_it_names(sprint_meeting):
    _meeting, applied = sprint_meeting
    assert applied["GuidanceEnabled"] is True and applied["WhiteboardsEnabled"] is True
    guidance = applied["Playbook"]["Guidance"]
    assert guidance["Checks"] == SPRINT_CHECKS
    assert "fact_check" not in guidance["Checks"] and "lookup" not in guidance["Checks"]
    assert applied["Playbook"]["Previous"] == {
        "GuidanceEnabled": False,
        "WhiteboardsEnabled": False,
    }


def test_its_quick_actions_are_listed_on_the_meeting(sprint_meeting):
    _meeting, applied = sprint_meeting
    labels = [a["Label"] for a in applied["Playbook"]["QuickActions"]]
    assert labels == ["Scope vs capacity", "Stories without owners", "Sprint summary"]
    assert all(a["Prompt"] for a in applied["Playbook"]["QuickActions"])


def test_a_user_who_does_not_own_the_meeting_is_refused(world, sprint_meeting):
    meeting, _applied = sprint_meeting
    body = world.call(
        world.other,
        APPLY,
        {"i": {"CallId": meeting.call_id, "SkillId": "retrospective"}},
    )
    assert body.get("errors")
    assert meeting.meeting()["Playbook"]["SkillId"] == "sprint-planning"


def test_switching_deletes_the_whiteboards_nobody_wrote_on_and_removing_restores(
    world, sprint_meeting
):
    meeting, _applied = sprint_meeting
    switched = world.data(
        world.owner,
        APPLY,
        {
            "i": {
                "CallId": meeting.call_id,
                "SkillId": "retrospective",
                "DeleteWhiteboards": True,
            }
        },
    )["applyMeetingPlaybook"]
    assert switched["Playbook"]["SkillId"] == "retrospective"
    assert [b["Template"] for b in world.boards(meeting)] == ["retrospective"]
    assert switched["Playbook"]["Previous"] == {
        "GuidanceEnabled": False,
        "WhiteboardsEnabled": False,
    }
    removed = world.data(
        world.owner, APPLY, {"i": {"CallId": meeting.call_id, "SkillId": None}}
    )["applyMeetingPlaybook"]
    assert removed["Playbook"] is None and removed["PlaybookUpdatedAt"]
    assert (
        removed["GuidanceEnabled"] is False and removed["WhiteboardsEnabled"] is False
    )
    assert [b["Template"] for b in world.boards(meeting)] == ["retrospective"], (
        "kept unless asked"
    )


# ── at a meeting's start ─────────────────────────────────────────────────────


def test_a_start_naming_a_playbook_gets_it_applied_once_the_meeting_exists(world):
    meeting = world.meeting(
        "incident-from-vp",
        PlaybookId="incident-review",
        CaptureSource="VIRTUAL_PARTICIPANT",
    )
    row = world.wait_playbook(meeting)
    assert row.get("Playbook", {}).get("SkillId") == "incident-review", row.get(
        "Playbook"
    )
    assert row["Playbook"]["Origin"] == "virtual-participant"
    assert sorted(b["Template"] for b in world.boards(meeting)) == sorted(
        SHIPPED_PLAYBOOKS["incident-review"]
    )


def test_a_playbook_that_no_longer_exists_is_noted_and_none_applied(world):
    meeting = world.meeting(
        "deleted-playbook", PlaybookId="integ-test-deleted-playbook"
    )
    row = world.wait_playbook(meeting)
    record = row.get("Playbook") or {}
    assert record.get("Requested") == "integ-test-deleted-playbook", record
    assert "SkillId" not in record and "no longer exists" in " ".join(
        record.get("Notes") or []
    )
    assert world.boards(meeting) == []


def test_none_at_the_start_applies_no_playbook(world):
    meeting = world.meeting("none-from-vp", PlaybookId="none")
    time.sleep(15)
    assert "Playbook" not in (meeting.meeting() or {})


# ── a scheduled Virtual Participant ──────────────────────────────────────────


def test_a_scheduled_vp_stores_its_playbook_and_an_edit_changes_it(client):
    vp_id = None
    try:
        created = client.appsync.graphql(
            CREATE_VP,
            {
                "input": {
                    "meetingName": "integ-test (meeting playbook, never launched)",
                    "meetingPlatform": "ZOOM",
                    "meetingId": "0000000000",
                    "isScheduled": True,
                    "status": "SCHEDULED",
                    "meetingTime": int(time.time()) + 24 * 3600,
                    "owner": "integ-test@lma.aws",
                    "playbook": "sprint-planning",
                }
            },
        )["createVirtualParticipant"]
        vp_id = created["id"]
        assert client.appsync.graphql(GET_VP, {"id": vp_id})["getVirtualParticipant"][
            "playbook"
        ] == ("sprint-planning")
        client.appsync.graphql(EDIT_VP, {"input": {"id": vp_id, "playbook": "none"}})
        assert (
            client.appsync.graphql(GET_VP, {"id": vp_id})["getVirtualParticipant"][
                "playbook"
            ]
            == "none"
        )
        client.appsync.graphql(EDIT_VP, {"input": {"id": vp_id, "playbook": None}})
        assert (
            client.appsync.graphql(GET_VP, {"id": vp_id})["getVirtualParticipant"][
                "playbook"
            ]
            is None
        )
        with pytest.raises(Exception, match="playbook must be"):
            client.appsync.graphql(
                EDIT_VP, {"input": {"id": vp_id, "playbook": "Not A Skill"}}
            )
    finally:
        if vp_id:
            client.vp.end(
                vp_id, reason="integ-test meeting playbook: cancelled before launch"
            )


# ── the profile's default (opt-in) ───────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("LMA_TEST_PLAYBOOK_DEFAULT") != "1",
    reason="changes the active profile's Meeting playbooks setting; set LMA_TEST_PLAYBOOK_DEFAULT=1",
)
def test_the_profiles_default_playbook_applies_to_a_new_meeting(world, client):
    from meeting_consent_probe import ConsentSettingsOverride, Stack  # pylint: disable=import-outside-toplevel

    override = ConsentSettingsOverride(
        Stack.find(client.stack_name, client.region),
        section="meeting_assist",
        updated_by="integ-tests (meeting playbooks)",
    )
    override.save()
    try:
        override.apply(playbooks={"defaultPlaybook": "retrospective"})
        # The call event processor reads the section on its standard tier, about
        # five minutes: a new meeting a minute apart until one has the default.
        deadline = time.monotonic() + DEFAULT_WAIT_SECONDS
        record: dict = {}
        while time.monotonic() < deadline and not record:
            meeting = world.meeting(
                "default-playbook", CaptureSource="VIRTUAL_PARTICIPANT"
            )
            record = world.wait_playbook(meeting, seconds=45).get("Playbook") or {}
            if not record:
                time.sleep(15)
        assert record.get("SkillId") == "retrospective", record
        assert record["Origin"] == "profile-default"
    finally:
        override.restore()
