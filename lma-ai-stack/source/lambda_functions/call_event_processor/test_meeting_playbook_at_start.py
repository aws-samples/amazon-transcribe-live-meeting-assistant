# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting playbook a new meeting is given (docs/meeting-playbooks.md).

A Virtual Participant's START names one (``PlaybookId``: a skill id, or "none");
without one, the profile's default playbook applies, except to an uploaded
recording. The id is taken off the event, which createCall would refuse, and
the playbook is applied once, after the meeting is created.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CONFIG_LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(HERE), str(CONFIG_LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

import test_graphql_response_handling as harness  # noqa: E402
from graphql import build_ast_schema, parse  # noqa: E402
from lma_config import shipped_setting_defaults  # noqa: E402

cep = harness.cep


@pytest.fixture(scope="module")
def schema():
    return build_ast_schema(
        parse(harness.APPSYNC_PRELUDE + harness.SCHEMA_FILE.read_text(encoding="utf-8")),
        assume_valid_sdl=True,
    )


@pytest.fixture()
def world(monkeypatch):
    state = {"default": "", "queued": []}

    def read_setting(section, setting, **kwargs):
        if setting == "playbooks":
            return {"defaultPlaybook": state["default"]}
        return kwargs.get("default")

    monkeypatch.setattr(cep, "read_setting", read_setting)
    monkeypatch.setattr(cep, "MEETING_WHITEBOARDS_ARN", "arn:whiteboards")
    monkeypatch.setattr(cep, "START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    monkeypatch.setattr(cep, "queue_playbook_apply", lambda *args: state["queued"].append(args))
    return state


def start(schema, **fields):
    session = harness.FakeSession(schema, {"createCall": {"CallId": "call-1", "Owner": "owner"}})
    harness.run(
        cep.execute_process_event_api_mutation(
            message={
                "EventType": "START",
                "CallId": "call-1",
                "AgentId": "owner",
                "CreatedAt": "2026-11-28T10:00:00Z",
                **fields,
            },
            appsync_session=session,
            agent_assist_args={},
            sentiment_analysis_args={},
        )
    )
    return harness.mutation_inputs(session, "createCall")


def test_the_shipped_default_is_no_playbook():
    assert shipped_setting_defaults()["meeting_assist"]["playbooks"] == {"defaultPlaybook": ""}


def test_without_a_choice_or_a_default_no_playbook_is_applied(schema, world):
    start(schema)
    assert world["queued"] == []


def test_a_virtual_participants_choice_is_applied_once_the_meeting_exists(schema, world):
    world["default"] = "retrospective"
    (sent,) = start(schema, PlaybookId="sprint-planning")
    assert "PlaybookId" not in sent  # not a CreateCallInput field
    assert world["queued"] == [("call-1", "sprint-planning", "virtual-participant", "owner")]


def test_the_profile_default_applies_to_a_meeting_that_names_none(schema, world):
    world["default"] = "retrospective"
    start(schema, CaptureSource="STREAM_AUDIO")
    assert world["queued"] == [("call-1", "retrospective", "profile-default", "owner")]


def test_none_overrides_the_profile_default(schema, world):
    world["default"] = "retrospective"
    (sent,) = start(schema, PlaybookId="none")
    assert "PlaybookId" not in sent and world["queued"] == []


def test_an_uploaded_recording_is_not_given_the_default(schema, world):
    world["default"] = "retrospective"
    start(schema, CaptureSource="UPLOAD")
    assert world["queued"] == []


@pytest.mark.parametrize("value", ["Not A Name", "../x"])
def test_anything_that_is_not_a_skill_id_is_no_playbook(world, value):
    world["default"] = value
    assert cep.playbook_for_start({"PlaybookId": value}) is None
    assert cep.default_playbook() == ""
