# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests of scripts/srt/setup.py: where SRT's placeholder AWS credentials may be written."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[2] / "srt" / "setup.py"
spec = importlib.util.spec_from_file_location("srt_setup", SETUP)
srt_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srt_setup)

RUNNER = {"GITLAB_CI": "true", "CI": "true"}


def test_srt_placeholder_credentials_are_written_only_on_a_ci_runner(tmp_path: Path) -> None:
    written = srt_setup.write_placeholder_credentials(RUNNER, tmp_path)
    assert written == tmp_path / ".aws" / "credentials"
    assert written.read_text() == srt_setup.PLACEHOLDER_CREDENTIALS


@pytest.mark.parametrize("env", [{}, {"CI": "true"}, {"CI": "1"}])
def test_a_bare_ci_variable_does_not_make_a_ci_runner(tmp_path: Path, env: dict) -> None:
    assert not srt_setup.on_ci_runner(env)
    assert srt_setup.write_placeholder_credentials(env, tmp_path) is None
    assert not (tmp_path / ".aws" / "credentials").exists()


@pytest.mark.parametrize("name", srt_setup.CREDENTIAL_ENV)
def test_no_placeholder_when_the_environment_names_a_credential_source(
    tmp_path: Path, name: str
) -> None:
    env = RUNNER | {name: "x"}
    assert srt_setup.write_placeholder_credentials(env, tmp_path) is None
    assert not (tmp_path / ".aws" / "credentials").exists()


@pytest.mark.parametrize("runner", [{"GITLAB_CI": "true"}, {"GITHUB_ACTIONS": "true"}])
def test_srt_placeholder_is_written_on_a_ci_runner_without_asking_instance_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: dict
) -> None:
    def no_network(*args, **kwargs):
        raise AssertionError("setup must not call out to decide on the placeholder")

    monkeypatch.setattr(srt_setup.urllib.request, "urlopen", no_network)
    written = srt_setup.write_placeholder_credentials(runner, tmp_path)
    assert written == tmp_path / ".aws" / "credentials"
    assert written.read_text() == srt_setup.PLACEHOLDER_CREDENTIALS


def test_no_placeholder_when_the_aws_config_has_its_own_credential_source(tmp_path: Path) -> None:
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[default]\ncredential_process = /bin/true\n")
    assert srt_setup.write_placeholder_credentials(RUNNER, tmp_path) is None


def test_existing_credentials_are_never_replaced(tmp_path: Path) -> None:
    (tmp_path / ".aws").mkdir()
    creds = tmp_path / ".aws" / "credentials"
    creds.write_text("[default]\nreal = yes\n")
    assert srt_setup.write_placeholder_credentials(RUNNER, tmp_path) is None
    srt_setup.remove_placeholder_credentials(creds)
    assert creds.read_text() == "[default]\nreal = yes\n"


def test_the_placeholder_is_removed_after_setup_and_nothing_else(tmp_path: Path) -> None:
    written = srt_setup.write_placeholder_credentials(RUNNER, tmp_path)
    srt_setup.remove_placeholder_credentials(written)
    assert not written.exists()
    srt_setup.remove_placeholder_credentials(None)


def test_setup_removes_the_placeholder_even_when_srt_config_fails() -> None:
    text = SETUP.read_text()
    body = text.split("placeholder = write_placeholder_credentials()", 1)[1]
    assert "finally:\n            remove_placeholder_credentials(placeholder)" in body


def test_the_scanner_venv_uses_the_python_version_ci_runs() -> None:
    ci = (Path(__file__).resolve().parents[3] / ".gitlab-ci.yml").read_text()
    assert f"library/python:{srt_setup.SCANNER_PYTHON_VERSION}-" in ci


def test_srt_python_overrides_the_path_lookup() -> None:
    assert srt_setup.scanner_python({"SRT_PYTHON": "/opt/py/bin/python3.13", "PATH": ""}) == "/opt/py/bin/python3.13"


def test_the_scanner_python_is_found_on_path(tmp_path: Path) -> None:
    exe = tmp_path / f"python{srt_setup.SCANNER_PYTHON_VERSION}"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert srt_setup.scanner_python({"PATH": str(tmp_path)}) == str(exe)
    assert srt_setup.scanner_python({"PATH": str(tmp_path / "none")}) is None


def test_the_venv_version_is_read_from_pyvenv_cfg(tmp_path: Path) -> None:
    assert srt_setup.venv_version(tmp_path) is None
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.25\n")
    assert srt_setup.venv_version(tmp_path) == "3.9"
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /x\nversion_info = 3.13.18.final.0\n")
    assert srt_setup.venv_version(tmp_path) == "3.13"


def test_srt_config_sees_the_chosen_interpreter_as_python3(tmp_path: Path) -> None:
    env = srt_setup.srt_env(tmp_path, "/opt/py/bin/python3.13", {"PATH": "/usr/bin"})
    shim = tmp_path / ".python-shim"
    assert env["PATH"].split(":")[0] == str(shim)
    assert (shim / "python3").readlink() == Path("/opt/py/bin/python3.13")
    assert (shim / "python").readlink() == Path("/opt/py/bin/python3.13")
    assert srt_setup.srt_env(tmp_path, None, {"PATH": "/usr/bin"})["PATH"] == "/usr/bin"
