# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for `lma skills`."""

import base64
import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lma_cli.cli import main

LIBRARY = {"kind": "lma-skill-library", "version": 1, "exportedAt": "x", "skills": [{"name": "a"}]}


@pytest.fixture
def runner():
    return CliRunner()


def _run(runner, args, client, **kwargs):
    return runner.invoke(main, ["skills", *args], obj={"client_factory": lambda **kw: client}, **kwargs)


def test_list_prints_each_skill(runner):
    client = MagicMock()
    client.skills.list.return_value = [
        {"SkillId": "launch-review", "Description": "Review a launch.", "Files": [{"Path": "a.md"}]}
    ]
    result = _run(runner, ["list"], client)
    assert result.exit_code == 0, result.output
    assert "launch-review: Review a launch. (1 reference file)" in result.output


def test_get_of_a_missing_skill_fails(runner):
    client = MagicMock()
    client.skills.get.return_value = None
    assert _run(runner, ["get", "nope"], client).exit_code == 1


def test_create_reads_the_instructions_and_reference_files(runner, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Do it.")
    ref = tmp_path / "checklist.md"
    ref.write_text("- Pricing")
    client = MagicMock()
    client.skills.create.return_value = {"SkillId": "launch-review", "Success": True}
    result = _run(
        runner,
        [
            "create", "--name", "launch-review", "--description", "d",
            "--instructions-file", str(body), "--file", f"reference/checklist.md={ref}",
            "--metadata", '{"trigger": "t"}',
        ],
        client,
    )
    assert result.exit_code == 0, result.output
    args, kwargs = client.skills.create.call_args
    assert args == ("launch-review", "d", "Do it.")
    assert kwargs["files"] == {"reference/checklist.md": "- Pricing"}
    assert kwargs["metadata"] == {"trigger": "t"}


def test_create_without_instructions_or_with_a_bad_file_option_is_refused(runner, tmp_path):
    client = MagicMock()
    assert _run(runner, ["create", "--name", "a", "--description", "d"], client).exit_code == 2
    body = tmp_path / "b.md"
    body.write_text("x")
    result = _run(
        runner,
        ["create", "--name", "a", "--description", "d", "--instructions-file", str(body), "--file", "nolocal"],
        client,
    )
    assert result.exit_code == 2
    client.skills.create.assert_not_called()


def test_update_sends_only_what_is_given(runner):
    client = MagicMock()
    result = _run(runner, ["update", "launch-review", "--description", "new"], client)
    assert result.exit_code == 0, result.output
    kwargs = client.skills.update.call_args.kwargs
    assert kwargs["description"] == "new"
    assert kwargs["instructions"] is None and kwargs["files"] is None and kwargs["name"] is None


def test_delete_asks_unless_yes(runner):
    client = MagicMock()
    assert _run(runner, ["delete", "a"], client, input="n\n").exit_code == 1
    client.skills.delete.assert_not_called()
    assert _run(runner, ["delete", "a", "--yes"], client).exit_code == 0
    client.skills.delete.assert_called_once_with("a")


def test_import_reports_the_files_left_out(runner, tmp_path):
    source = tmp_path / "s.zip"
    source.write_bytes(b"PK")
    client = MagicMock()
    client.skills.import_file.return_value = {
        "Success": True,
        "Message": "Skill s created; 1 file left out",
        "Skipped": [{"Path": "run.py", "Reason": "only text and Markdown reference files are kept"}],
        "Notes": [],
    }
    result = _run(runner, ["import", str(source), "--replace"], client)
    assert result.exit_code == 0, result.output
    assert client.skills.import_file.call_args.kwargs["overwrite"] is True
    assert "left out run.py: only text and Markdown" in result.output


def test_a_refused_import_exits_non_zero(runner, tmp_path):
    from lma_sdk.exceptions import LMAAppSyncError

    source = tmp_path / "a.md"
    source.write_text("x")
    client = MagicMock()
    client.skills.import_file.side_effect = LMAAppSyncError("no front matter")
    result = _run(runner, ["import", str(source)], client)
    assert result.exit_code == 1 and "no front matter" in result.output


def test_export_writes_a_zip_to_its_own_name_by_default(runner, tmp_path):
    client = MagicMock()
    client.skills.export.return_value = {
        "fileName": "a.zip", "encoding": "base64", "content": base64.b64encode(b"zip").decode()
    }
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = _run(runner, ["export", "a", "--format", "zip"], client)
        assert result.exit_code == 0, result.output
        with open("a.zip", "rb") as handle:
            assert handle.read() == b"zip"


def test_export_library_to_standard_output_and_import_library_from_a_file(runner, tmp_path):
    client = MagicMock()
    client.skills.export_library.return_value = LIBRARY
    result = _run(runner, ["export-library"], client)
    assert json.loads(result.output) == LIBRARY
    source = tmp_path / "skills.json"
    source.write_text(json.dumps(LIBRARY))
    client.skills.import_library.return_value = {
        "Success": True, "Message": "1 of 1 skills processed", "Skills": [{"Name": "a", "Action": "created"}]
    }
    result = _run(runner, ["import-library", str(source)], client)
    assert result.exit_code == 0, result.output
    assert client.skills.import_library.call_args.args[0] == LIBRARY
    assert "a: created" in result.output


def test_import_library_of_a_file_that_is_not_json_or_not_a_library(runner, tmp_path):
    source = tmp_path / "x.json"
    source.write_text("nope")
    client = MagicMock()
    assert _run(runner, ["import-library", str(source)], client).exit_code == 2
    source.write_text("{}")
    client.skills.import_library.side_effect = ValueError("not a skill library")
    assert _run(runner, ["import-library", str(source)], client).exit_code == 2


def test_a_failed_library_import_exits_non_zero(runner, tmp_path):
    source = tmp_path / "skills.json"
    source.write_text(json.dumps(LIBRARY))
    client = MagicMock()
    client.skills.import_library.return_value = {"Success": False, "Message": "x", "Skills": []}
    assert _run(runner, ["import-library", str(source)], client).exit_code == 1
