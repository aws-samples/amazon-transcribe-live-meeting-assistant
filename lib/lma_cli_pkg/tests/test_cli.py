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


class TestVpCreateCommand:
    """Tests for `lma vp create`."""

    def _invoke(self, runner, args):
        from lma_sdk.models.virtual_participant import VpLaunchResult

        mock_client = MagicMock()
        mock_client.vp.create.return_value = VpLaunchResult(
            id="vp-1", status="INITIALIZING", meeting_name="m",
            meeting_platform="ZOOM", meeting_id="1",
            owner="alice@example.com", user_sub="sub-1", zoom_credentials_used=True,
        )
        result = runner.invoke(
            main,
            ["vp", "create", "--name", "m", "--platform", "zoom", "--id", "1", "--no-wait", *args],
            obj={"client_factory": lambda **kw: mock_client},
        )
        return result, mock_client

    def test_without_as_user_launches_with_no_user(self, runner):
        result, mock_client = self._invoke(runner, [])
        assert result.exit_code == 0, result.output
        kwargs = mock_client.vp.create.call_args.kwargs
        assert kwargs["as_user"] is None
        assert kwargs["use_zoom_credentials"] is None
        assert kwargs["user_name"] == "lma-cli@lma"

    def test_as_user_is_passed_through(self, runner):
        result, mock_client = self._invoke(runner, ["--as-user", "alice@example.com"])
        assert result.exit_code == 0, result.output
        kwargs = mock_client.vp.create.call_args.kwargs
        assert kwargs["as_user"] == "alice@example.com"
        assert kwargs["user_name"] is None  # SDK defaults it to the user's email
        assert "alice@example.com" in result.output
        assert "stored credentials" in result.output

    def test_zoom_credentials_flag_is_passed_through(self, runner):
        result, mock_client = self._invoke(
            runner, ["--as-user", "alice@example.com", "--no-zoom-credentials"],
        )
        assert result.exit_code == 0, result.output
        assert mock_client.vp.create.call_args.kwargs["use_zoom_credentials"] is False

    def test_zoom_credentials_flag_requires_as_user(self, runner):
        result, mock_client = self._invoke(runner, ["--zoom-credentials"])
        assert result.exit_code == 2
        mock_client.vp.create.assert_not_called()
