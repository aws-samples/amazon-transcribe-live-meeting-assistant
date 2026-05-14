# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Smoke-test the click CLI — help screens, --dry-run paths."""

from __future__ import annotations

from click.testing import CliRunner

from lma_load.cli import _parse_duration, load_command


def test_cli_shows_help():
    runner = CliRunner()
    result = runner.invoke(load_command, ["--help"])
    assert result.exit_code == 0
    assert "LMA Load Simulator" in result.output
    for sub in ("concurrent", "backfill", "rbac", "cleanup", "stack-info"):
        assert sub in result.output


def test_cli_concurrent_requires_driver():
    runner = CliRunner()
    result = runner.invoke(load_command, ["concurrent", "--help"])
    assert result.exit_code == 0
    assert "--driver" in result.output
    assert "kinesis" in result.output
    assert "websocket" in result.output
    assert "vp" in result.output


def test_cli_rbac_help():
    runner = CliRunner()
    result = runner.invoke(load_command, ["rbac", "--help"])
    assert result.exit_code == 0
    assert "--users" in result.output
    assert "--email-prefix" in result.output
    assert "--email-domain" in result.output


def test_cli_cleanup_help_has_target_run_id():
    runner = CliRunner()
    result = runner.invoke(load_command, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--target-run-id" in result.output


def test_parse_duration_formats():
    assert _parse_duration("30s") == 30.0
    assert _parse_duration("5m") == 300.0
    assert _parse_duration("1h") == 3600.0
    assert _parse_duration("500ms") == 0.5
    assert _parse_duration("42") == 42.0
