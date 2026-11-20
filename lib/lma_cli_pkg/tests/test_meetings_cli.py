# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for `lma meetings delete`.

The SDK is a stand-in: what is asserted here is the command's contract -- a dry
run unless --confirm is given, the labels parsed and passed on, a final count,
and a non-zero exit when any meeting could not be deleted. Which meetings are
found and how each is deleted is tested in lib/lma_sdk/tests/test_meeting_operations.py.
"""

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lma_cli.cli import main
from lma_sdk.models.meetings import MeetingDeleteResult, MeetingFailure, MeetingRef


@pytest.fixture
def runner():
    return CliRunner()


def _run(runner, args, client):
    return runner.invoke(main, ["meetings", "delete", *args], obj={"client_factory": lambda **kw: client})


def _result(**overrides):
    base = dict(
        dry_run=True,
        user="alice",
        scope="own",
        labels={"engagement": "acme"},
        matched=[MeetingRef(call_id="acme-1", owner="alice"), MeetingRef(call_id="acme-2", owner="alice")],
        skipped_in_progress=[MeetingRef(call_id="acme-live", owner="alice", status="TRANSCRIBING")],
    )
    base.update(overrides)
    return MeetingDeleteResult(**base)


def test_without_confirm_it_is_a_dry_run(runner):
    client = MagicMock()
    client.meetings.delete_by_labels.return_value = _result()
    result = _run(runner, ["--label", "engagement=acme", "--user-name", "alice"], client)
    assert result.exit_code == 0, result.output
    client.meetings.delete_by_labels.assert_called_once_with(
        {"engagement": "acme"}, as_user="alice", confirm=False
    )
    assert "Dry run: 2 meeting(s) would be deleted. Nothing was deleted" in result.output
    assert "acme-live" in result.output and "still in progress" in result.output


def test_with_confirm_it_deletes_and_prints_the_count(runner):
    client = MagicMock()
    client.meetings.delete_by_labels.return_value = _result(
        dry_run=False, deleted=["acme-1", "acme-2"]
    )
    result = _run(runner, ["--label", "engagement=acme", "--user-name", "alice", "--confirm"], client)
    assert result.exit_code == 0, result.output
    assert client.meetings.delete_by_labels.call_args.kwargs["confirm"] is True
    assert "Deleted 2 of 2 meeting(s)." in result.output


def test_a_failure_is_named_and_the_command_exits_non_zero(runner):
    client = MagicMock()
    client.meetings.delete_by_labels.return_value = _result(
        dry_run=False,
        deleted=["acme-1"],
        failed=[MeetingFailure(call_id="acme-2", error="the delete failed")],
    )
    result = _run(runner, ["--label", "engagement=acme", "--user-name", "alice", "--confirm"], client)
    assert result.exit_code == 1
    assert "acme-2: the delete failed" in result.output
    assert "Deleted 1 of 2 meeting(s); 1 failed." in result.output


def test_repeated_labels_must_all_match(runner):
    client = MagicMock()
    client.meetings.delete_by_labels.return_value = _result()
    _run(runner, ["--label", "engagement=acme", "--label", "phase=two", "--user-name", "a"], client)
    assert client.meetings.delete_by_labels.call_args.args[0] == {"engagement": "acme", "phase": "two"}


def test_an_admins_selection_says_it_covers_every_user(runner):
    client = MagicMock()
    client.meetings.delete_by_labels.return_value = _result(scope="all", user="admin")
    result = _run(runner, ["--label", "engagement=acme", "--user-name", "admin"], client)
    assert "every user's meetings" in result.output


@pytest.mark.parametrize("label", ["engagement", "=acme"])
def test_a_malformed_label_is_refused_before_anything_is_read(runner, label):
    client = MagicMock()
    result = _run(runner, ["--label", label, "--user-name", "alice"], client)
    assert result.exit_code == 2
    client.meetings.delete_by_labels.assert_not_called()


def test_the_user_and_a_label_are_required(runner):
    client = MagicMock()
    assert _run(runner, ["--label", "engagement=acme"], client).exit_code == 2
    assert _run(runner, ["--user-name", "alice"], client).exit_code == 2
    client.meetings.delete_by_labels.assert_not_called()


def test_json_output_is_the_result(runner):
    client = MagicMock()
    client.meetings.delete_by_labels.return_value = _result()
    result = _run(runner, ["--label", "engagement=acme", "--user-name", "alice", "--json"], client)
    assert result.exit_code == 0
    assert json.loads(result.output)["dry_run"] is True
