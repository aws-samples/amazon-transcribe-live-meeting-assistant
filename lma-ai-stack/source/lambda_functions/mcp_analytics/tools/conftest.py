# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Fixtures for the meeting tool tests: the tables, AppSync and Lambda, faked.

Nothing here reaches AWS. A tool's reads of the EventSourcing and Virtual
Participant tables are answered from lists of rows; its AppSync requests and
Lambda invocations are recorded and answered with what a test sets. The fakes
themselves are in _meeting_fakes.py.
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# The configuration layer the deployed function carries (lma_speakers,
# lma_chat_links), and the function's own directory for `tools.*`.
HERE = Path(__file__).resolve().parent
LAYER = HERE.parents[2] / "lambda_layers" / "configuration_layer"
for _path in (str(LAYER), str(HERE.parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools._meeting_fakes import FakeTable, Recorder  # noqa: E402


@pytest.fixture
def calls_table(monkeypatch):
    """The EventSourcing table every tool reads."""
    from tools import access, transcript_rows

    table = FakeTable()
    for module in (access, transcript_rows):
        monkeypatch.setattr(module, "calls_table", lambda: table)
    return table


@pytest.fixture
def vp_table(monkeypatch):
    """The Virtual Participant table."""
    from tools import virtual_participants

    table = FakeTable()
    monkeypatch.setattr(virtual_participants, "vp_table", lambda: table)
    return table


@pytest.fixture
def appsync(monkeypatch):
    """AppSync, for every module that sends a request."""
    from tools import (
        guidance,
        meeting_actions,
        meeting_agenda,
        meeting_record,
        notifications,
        virtual_participants,
    )

    recorder = Recorder()
    for module in (
        guidance,
        meeting_actions,
        meeting_agenda,
        meeting_record,
        notifications,
        virtual_participants,
    ):
        monkeypatch.setattr(module, "graphql", recorder.graphql)
    return recorder


@pytest.fixture
def lambdas(monkeypatch):
    """Direct Lambda invocations."""
    from tools import ask_assistant, meeting_actions

    recorder = Recorder()
    for module in (ask_assistant, meeting_actions):
        monkeypatch.setattr(module, "invoke_function", recorder.invoke)
    monkeypatch.setenv("MEETING_CONTROLS_FUNCTION", "controls-fn")
    monkeypatch.setenv("SUMMARY_REGENERATOR_FUNCTION", "summary-fn")
    monkeypatch.setenv("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN", "assistant-fn")
    return recorder
