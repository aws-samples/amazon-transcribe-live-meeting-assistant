# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for scripts/ux_test_session.py.

The script creates a real Cognito user in a live pool, so the properties worth
pinning are the safety ones: the throwaway user is unmistakably throwaway, the app
client is never touched, teardown deletes only what setup made, and the stack
outputs it reads are ones the LMA root template really declares. Nothing here
calls AWS: the module's ``aws`` function is replaced.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent


def _load():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("ux_test_session", SCRIPTS / "ux_test_session.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uts = _load()
CTX = {
    "stack": "s",
    "region": "us-east-1",
    "url": "https://example.test/",
    "user_pool": "us-east-1_Abc123",
}


def _stack(outputs: dict[str, str], parent: str | None = None) -> dict:
    info = {"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}
    if parent:
        info["ParentId"] = parent
    return {"Stacks": [info]}


GOOD_OUTPUTS = {
    "ApplicationCloudfrontEndpoint": "https://d111111abcdef8.cloudfront.net/",
    "CognitoUserPoolTokenIssuerUrl": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Abc123",
}


@pytest.mark.unit
class TestTheStackOutputsItReads:
    def test_the_outputs_are_declared_by_the_root_template(self):
        """A renamed output would otherwise surface only against a live stack."""
        text = (REPO_ROOT / "lma-main.yaml").read_text(encoding="utf-8")
        outputs = text[text.index("\nOutputs:") :]
        for name in (uts.WEB_URL_OUTPUT, uts.ISSUER_URL_OUTPUT):
            assert re.search(rf"^  {name}:$", outputs, re.M), name

    def test_url_and_pool_are_read_from_the_outputs(self):
        with patch.object(uts, "aws", return_value=_stack(GOOD_OUTPUTS)):
            ctx = uts.resolve_stack("s", "us-east-1")
        assert ctx["url"] == "https://d111111abcdef8.cloudfront.net/"
        assert ctx["user_pool"] == "us-east-1_Abc123"

    def test_a_stack_without_the_web_url_fails_with_an_explanation(self):
        outputs = {"CognitoUserPoolTokenIssuerUrl": GOOD_OUTPUTS["CognitoUserPoolTokenIssuerUrl"]}
        with patch.object(uts, "aws", return_value=_stack(outputs)):
            with pytest.raises(RuntimeError, match="ApplicationCloudfrontEndpoint"):
                uts.resolve_stack("s", "us-east-1")

    def test_an_unreadable_issuer_url_is_refused_rather_than_guessed(self):
        outputs = dict(GOOD_OUTPUTS, CognitoUserPoolTokenIssuerUrl="https://example.test/")
        with patch.object(uts, "aws", return_value=_stack(outputs)):
            with pytest.raises(RuntimeError, match="user pool id"):
                uts.resolve_stack("s", "us-east-1")

    def test_a_nested_stack_is_refused_with_what_to_pass_instead(self):
        with patch.object(uts, "aws", return_value=_stack(GOOD_OUTPUTS, parent="arn:parent")):
            with pytest.raises(RuntimeError, match="root LMA stack"):
                uts.resolve_stack("s-AISTACK-1", "us-east-1")


@pytest.mark.unit
class TestRoles:
    def test_the_groups_are_read_from_the_cognito_template(self):
        assert uts.template_group_names() == ["Admin"]

    def test_the_groupless_user_role_is_offered(self):
        assert uts.role_names() == ["Admin", "User"]

    def test_a_user_persona_is_created_in_no_group(self):
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "create_cognito_user") as create,
        ):
            uts.cmd_setup(SimpleNamespace(stack="s", group="User", region="us-east-1"))
        assert create.call_args.args[2] is None

    def test_an_admin_persona_is_created_in_the_admin_group(self):
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "create_cognito_user") as create,
        ):
            uts.cmd_setup(SimpleNamespace(stack="s", group="Admin", region="us-east-1"))
        assert create.call_args.args[2] == "Admin"


@pytest.mark.unit
class TestThrowawayCredentials:
    def test_the_default_email_can_never_reach_a_real_mailbox(self):
        """RFC 2606 reserves .invalid, so a stray invite cannot be delivered."""
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "create_cognito_user") as create,
        ):
            uts.cmd_setup(SimpleNamespace(stack="s", group="Admin", region="us-east-1"))
        email = create.call_args.args[1]
        assert email.startswith("ux-test-") and email.endswith("@example.invalid")

    def test_an_email_domain_replaces_only_the_domain(self):
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "create_cognito_user") as create,
        ):
            args = SimpleNamespace(
                stack="s", group="Admin", region="us-east-1", email_domain="@example.com"
            )
            uts.cmd_setup(args)
        email = create.call_args.args[1]
        assert re.fullmatch(r"ux-test-[0-9a-f]{8}@example\.com", email), email

    def test_the_password_satisfies_the_pool_policy(self):
        """lma-cognito-stack requires length 8+, upper, lower, digit and symbol."""
        for _ in range(20):
            password = uts._password()
            assert len(password) >= 8
            assert any(c.isupper() for c in password)
            assert any(c.islower() for c in password)
            assert any(c.isdigit() for c in password)
            assert any(not c.isalnum() for c in password)

    def test_passwords_are_not_reused_between_sessions(self):
        assert len({uts._password() for _ in range(50)}) == 50

    def test_invitation_is_suppressed_and_the_password_set_permanent(self):
        calls = []
        with patch.object(uts, "aws", side_effect=lambda *a, **k: calls.append((a, k))):
            uts.create_cognito_user(CTX, "ux-test-1@example.invalid", "Admin", "Ux!pw9aZ")
        (create, _), (set_password, set_kwargs), (add_to_group, _) = calls
        assert create[1] == "admin-create-user" and "SUPPRESS" in create
        assert set_password[1] == "admin-set-user-password"
        assert json.loads(set_kwargs["stdin"])["Permanent"] is True
        assert add_to_group[1] == "admin-add-user-to-group" and "Admin" in add_to_group

    def test_the_password_is_passed_on_standard_input_not_in_the_arguments(self):
        calls = []
        with patch.object(uts, "aws", side_effect=lambda *a, **k: calls.append((a, k))):
            uts.create_cognito_user(CTX, "ux-test-1@example.invalid", "Admin", "Ux!pw9aZ")
        set_password, set_kwargs = calls[1]
        assert "Ux!pw9aZ" not in set_password
        assert "file:///dev/stdin" in set_password
        assert json.loads(set_kwargs["stdin"])["Password"] == "Ux!pw9aZ"

    def test_a_cli_failure_never_repeats_the_password(self):
        """An error message names only the service and operation."""
        failed = SimpleNamespace(returncode=255, stdout="", stderr="InvalidPasswordException")
        with patch.object(uts.subprocess, "run", return_value=failed):
            with pytest.raises(RuntimeError) as excinfo:
                uts.aws("cognito-idp", "admin-set-user-password", "--password", "Ux!secret9aZ")
        assert "Ux!secret9aZ" not in str(excinfo.value)
        assert "admin-set-user-password" in str(excinfo.value)

    def test_a_domain_refusal_says_to_pass_the_allowed_domain(self):
        refusal = RuntimeError("UserLambdaValidationException: PreSignUp failed")
        with patch.object(uts, "aws", side_effect=refusal):
            with pytest.raises(SystemExit, match="--email-domain"):
                uts.create_cognito_user(CTX, "ux-test-1@example.invalid", None, "pw")

    def test_setup_reports_its_own_teardown_command(self, capsys):
        """An unrun teardown is what leaves a known password on the stack."""
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "create_cognito_user"),
        ):
            uts.cmd_setup(SimpleNamespace(stack="my-stack", group="Admin", region="us-west-2"))
        printed = capsys.readouterr().out
        assert "teardown my-stack" in printed
        assert "--email ux-test-" in printed
        assert "--region us-west-2" in printed


@pytest.mark.unit
class TestTheAppClientIsNeverWidened:
    def test_no_command_touches_the_app_client(self):
        """setup needs only user-pool admin APIs; the browser signs in over SRP."""
        source = (SCRIPTS / "ux_test_session.py").read_text(encoding="utf-8")
        code = source.split('"""', 2)[2]  # past the module docstring, which explains why
        for call in (
            "update-user-pool-client",
            "admin-initiate-auth",
            "ALLOW_ADMIN_USER_PASSWORD_AUTH",
        ):
            assert call not in code, call


@pytest.mark.unit
class TestTeardown:
    def test_teardown_deletes_the_named_user(self):
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "delete_cognito_user") as delete,
        ):
            args = SimpleNamespace(
                stack="s", email="ux-test-1@example.invalid", stale=None, region="us-east-1"
            )
            assert uts.cmd_teardown(args) == 0
        delete.assert_called_once_with(CTX, "ux-test-1@example.invalid")

    def test_teardown_needs_an_email_when_not_sweeping(self):
        args = SimpleNamespace(stack="s", email=None, stale=None, region="us-east-1")
        with pytest.raises(SystemExit, match="--email"):
            uts.cmd_teardown(args)

    def test_teardown_refuses_a_user_setup_did_not_make(self):
        """A copied command edited to another address must not delete a real user."""
        with patch.object(uts, "_resolve_or_explain") as resolve:
            args = SimpleNamespace(
                stack="s", email="operator@example.com", stale=None, region="us-east-1"
            )
            with pytest.raises(SystemExit, match="ux-test-"):
                uts.cmd_teardown(args)
        resolve.assert_not_called()


@pytest.mark.unit
class TestStaleSweep:
    def test_only_ux_test_users_on_the_session_domain_are_selected(self):
        old = "2020-01-01T00:00:00Z"
        users = {
            "Users": [
                {"Username": "operator@example.com", "UserCreateDate": old},
                {"Username": "ux-test-lead@example.com", "UserCreateDate": old},
                {"Username": "admin@example.invalid", "UserCreateDate": old},
                {"Username": "ux-test-abcd1234@example.invalid", "UserCreateDate": old},
            ]
        }
        with patch.object(uts, "aws", return_value=users):
            assert uts._stale_ux_users(CTX, older_than_hours=1) == [
                "ux-test-abcd1234@example.invalid"
            ]
            assert uts._stale_ux_users(CTX, 1, "example.com") == ["ux-test-lead@example.com"]

    def test_a_session_younger_than_the_cutoff_is_left_alone(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        users = {
            "Users": [
                {
                    "Username": "ux-test-inuse01@example.invalid",
                    "UserCreateDate": recent.isoformat().replace("+00:00", "Z"),
                }
            ]
        }
        with patch.object(uts, "aws", return_value=users):
            assert uts._stale_ux_users(CTX, older_than_hours=12) == []

    def test_sweeping_deletes_every_stale_user_it_found(self):
        stale = ["ux-test-a@example.invalid", "ux-test-b@example.invalid"]
        with (
            patch.object(uts, "_resolve_or_explain", return_value=CTX),
            patch.object(uts, "_stale_ux_users", return_value=stale),
            patch.object(uts, "delete_cognito_user") as delete,
        ):
            args = SimpleNamespace(stack="s", email=None, stale=12.0, region="us-east-1")
            assert uts.cmd_teardown(args) == 0
        assert [c.args[1] for c in delete.call_args_list] == stale


@pytest.mark.unit
class TestWrongStackOrRegion:
    def test_a_missing_stack_names_the_region_and_suggests_alternatives(self):
        with (
            patch.object(
                uts, "resolve_stack", side_effect=RuntimeError("Stack with id X does not exist")
            ),
            patch.object(uts, "_candidate_stacks", return_value=["LMA-dev-1", "lma-other"]),
        ):
            with pytest.raises(SystemExit) as excinfo:
                uts._resolve_or_explain("LMA-typo", "us-east-1")
        message = str(excinfo.value)
        assert "us-east-1" in message and "--region" in message and "LMA-dev-1" in message

    def test_no_candidates_points_at_region_and_profile(self):
        with (
            patch.object(
                uts, "resolve_stack", side_effect=RuntimeError("Stack with id X does not exist")
            ),
            patch.object(uts, "_candidate_stacks", return_value=[]),
        ):
            with pytest.raises(SystemExit) as excinfo:
                uts._resolve_or_explain("LMA-typo", "eu-west-1")
        assert "AWS_PROFILE" in str(excinfo.value)

    def test_an_unrelated_failure_is_not_swallowed(self):
        with patch.object(uts, "resolve_stack", side_effect=RuntimeError("AccessDenied")):
            with pytest.raises(RuntimeError, match="AccessDenied"):
                uts._resolve_or_explain("s", "us-east-1")

    def test_suggestions_never_break_the_error_message(self):
        with patch.object(uts, "aws", side_effect=RuntimeError("boom")):
            assert uts._candidate_stacks("us-east-1") == []

    def test_candidates_are_the_root_stacks_named_like_lma(self):
        with patch.object(uts, "aws", return_value="LMA-dev\tother-app\tmy-lma-test"):
            assert uts._candidate_stacks("us-east-1") == ["LMA-dev", "my-lma-test"]
