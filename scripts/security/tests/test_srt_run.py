# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests of scripts/srt/run.py: the environment the assessment runs in."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

RUN = Path(__file__).resolve().parents[2] / "srt" / "run.py"
spec = importlib.util.spec_from_file_location("srt_run", RUN)
srt_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srt_run)


def test_srt_scanner_venv_comes_first_on_path(tmp_path: Path) -> None:
    env = srt_run.scan_env(tmp_path, {"PATH": "/home/u/.local/bin:/usr/bin", "HOME": "/home/u"})
    assert env["PATH"].split(os.pathsep) == [
        str(tmp_path / ".venv" / "bin"),
        "/home/u/.local/bin",
        "/usr/bin",
    ]
    assert env["HOME"] == "/home/u"


def test_srt_scanner_venv_is_the_path_when_none_is_set(tmp_path: Path) -> None:
    assert srt_run.scan_env(tmp_path, {})["PATH"] == str(tmp_path / ".venv" / "bin")


def test_the_assessment_runs_with_that_environment() -> None:
    assert "env=scan_env(srt_dir)" in RUN.read_text()
