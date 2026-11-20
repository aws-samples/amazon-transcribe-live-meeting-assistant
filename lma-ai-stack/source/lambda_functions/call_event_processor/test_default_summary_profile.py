# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The summary profile a meeting that starts without one is given.

The Meeting Processing section's defaultSummaryProfile, shipped as ``default``
(no profile, so nothing changes). Another name is written into the meeting's
createCall input when its START event names no profile, so the meeting page shows
it and its owner can change it before the summary. Every capture method's meeting,
uploads included, is created by a START event in this function.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
CONFIG_LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(HERE), str(CONFIG_LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

# The harness, not its tests or fixtures, which would be collected a second time.
import test_graphql_response_handling as harness  # noqa: E402
from graphql import build_ast_schema, parse  # noqa: E402
from lma_config import parameters, shipped_setting_defaults  # noqa: E402

cep = harness.cep
FakeSession = harness.FakeSession
mutation_inputs = harness.mutation_inputs
run = harness.run


@pytest.fixture(scope="module")
def schema():
    """The schema the stack deploys, as the harness builds it."""
    return build_ast_schema(
        parse(harness.APPSYNC_PRELUDE + harness.SCHEMA_FILE.read_text(encoding="utf-8")),
        assume_valid_sdl=True,
    )


class _Store:
    def __init__(self, sections):
        self.sections = sections

    def get_section(self, name):
        return SimpleNamespace(values=self.sections.get(name, {}))


@pytest.fixture()
def configured(monkeypatch):
    def install(profile=None):
        values = {} if profile is None else {"defaultSummaryProfile": profile}
        monkeypatch.setattr(parameters, "_SHARED_STORE", _Store({"meeting_processing": values}))

    install()
    return install


def created_with(schema, monkeypatch, **fields):
    """The createCall input a START event with ``fields`` is created with."""
    monkeypatch.setattr(cep, "START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    monkeypatch.setattr(cep, "SETTINGS", {}, raising=False)
    session = FakeSession(schema, {"createCall": {"CallId": "call-1", "Owner": "owner"}})
    run(
        cep.execute_process_event_api_mutation(
            message={
                "EventType": "START",
                "CallId": "call-1",
                "AgentId": "owner",
                "CreatedAt": "2026-11-20T10:00:00Z",
                **fields,
            },
            appsync_session=session,
            agent_assist_args={},
            sentiment_analysis_args={},
        )
    )
    (sent,) = mutation_inputs(session, "createCall")
    return sent


def test_the_shipped_default_is_no_profile(configured):
    assert shipped_setting_defaults()["meeting_processing"]["defaultSummaryProfile"] == "default"
    assert cep.default_summary_profile() is None


def test_with_the_shipped_default_a_meeting_starts_with_no_profile(schema, configured, monkeypatch):
    assert "SummaryProfile" not in created_with(schema, monkeypatch)


def test_a_configured_default_is_given_to_a_meeting_that_names_none(
    schema, configured, monkeypatch
):
    configured("discovery-session")
    assert created_with(schema, monkeypatch)["SummaryProfile"] == "discovery-session"


@pytest.mark.parametrize("source", ["STREAM_AUDIO", "DESKTOP_APP", "UPLOAD", "VIRTUAL_PARTICIPANT"])
def test_every_capture_method_is_given_it(schema, configured, monkeypatch, source):
    configured("discovery-session")
    sent = created_with(schema, monkeypatch, CaptureSource=source)
    assert sent["SummaryProfile"] == "discovery-session"


def test_a_meeting_that_names_a_profile_keeps_it(schema, configured, monkeypatch):
    configured("discovery-session")
    assert created_with(schema, monkeypatch, SummaryProfile="healthcare")["SummaryProfile"] == (
        "healthcare"
    )


@pytest.mark.parametrize("value", ["default", "", "Not A Name", 7, None])
def test_default_or_anything_not_a_profile_name_gives_no_profile(monkeypatch, value):
    monkeypatch.setattr(cep, "read_setting", lambda *_args, **_kw: value)
    assert cep.default_summary_profile() is None
    assert cep.with_default_summary_profile({"CallId": "c"}) == {"CallId": "c"}


def test_the_upload_path_creates_its_meeting_with_a_start_event():
    """So the one place the default is given covers uploaded recordings too."""
    source = (HERE.parent / "upload_meeting_processor" / "index.py").read_text(encoding="utf-8")
    assert '"EventType": "START"' in source
