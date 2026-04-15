# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for LMA CLI commands."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lma_cli.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestMainGroup:
    """Tests for the main CLI group."""

    def test_help(self, runner):
        """--help shows usage info."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "LMA CLI" in result.output
        assert "publish" in result.output
        assert "deploy" in result.output
        assert "status" in result.output

    def test_version(self, runner):
        """--version shows version."""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "lma-cli" in result.output

    def test_no_command_shows_help(self, runner):
        """Running without command shows help."""
        result = runner.invoke(main, [])
        assert result.exit_code == 0
        assert "LMA CLI" in result.output


class TestStatusCommand:
    """Tests for status command."""

    def test_status_not_found(self, runner):
        """status shows warning when stack not found."""
        mock_client = MagicMock()
        from lma_sdk.models.stack import StackStatusResult
        mock_client.stack.status.return_value = StackStatusResult(
            success=True, exists=False, message="Stack not found."
        )

        result = runner.invoke(main, ["status"], obj={
            "client_factory": lambda **kw: mock_client,
        })
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "Stack" in result.output


class TestDeployCommand:
    """Tests for deploy command."""

    def test_deploy_requires_admin_email_for_new_stack(self, runner):
        """deploy fails without --admin-email when creating a new stack."""
        mock_client = MagicMock()
        mock_client.stack.check_in_progress.return_value = None
        mock_client.stack.exists.return_value = False
        mock_client.stack_name = "LMA"

        result = runner.invoke(main, ["deploy"], obj={
            "client_factory": lambda **kw: mock_client,
        })
        assert result.exit_code != 0


class TestCheckPrereqs:
    """Tests for check-prereqs command."""

    @patch("lma_sdk._core.publish.check_prerequisites")
    def test_all_pass(self, mock_check, runner):
        """check-prereqs succeeds when all ok."""
        mock_check.return_value = []
        result = runner.invoke(main, ["check-prereqs"])
        assert result.exit_code == 0
        assert "prerequisites met" in result.output.lower() or "✓" in result.output

    @patch("lma_sdk._core.publish.check_prerequisites")
    def test_some_fail(self, mock_check, runner):
        """check-prereqs fails when prereqs missing."""
        mock_check.return_value = ["Docker not found"]
        result = runner.invoke(main, ["check-prereqs"])
        assert result.exit_code != 0
