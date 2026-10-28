# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for client.vp launches made for an LMA user (``as_user``)."""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from lma_sdk.exceptions import LMAConfigurationError, LMAVirtualParticipantError
from lma_sdk.models.stack import StackOutput
from lma_sdk.operations.virtual_participant import _extract_user_pool_id

POOL_ID = "us-east-1_AbCdEf123"
SUB = "11111111-2222-3333-4444-555555555555"
SFN_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test-lma-stack-LMAVirtualParticipantScheduler"


def _client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _cognito_user(username="alice@example.com", sub=SUB, email="alice@example.com", enabled=True):
    return {
        "Username": username,
        "Enabled": enabled,
        "UserAttributes": [
            {"Name": "sub", "Value": sub},
            {"Name": "email", "Value": email},
        ],
    }


@pytest.fixture
def services(client, mock_session):
    """Wire per-service mocks onto the client's session and namespaces."""
    cognito = MagicMock(name="cognito-idp")
    secrets = MagicMock(name="secretsmanager")
    sfn = MagicMock(name="stepfunctions")
    sfn.start_sync_execution.return_value = {
        "status": "SUCCEEDED",
        "executionArn": "arn:exec",
        "output": json.dumps({"tasks": [{"taskArn": "t"}]}),
    }
    by_name = {"cognito-idp": cognito, "secretsmanager": secrets, "stepfunctions": sfn}
    mock_session.client.side_effect = lambda name, **kw: by_name[name]

    stack = MagicMock(name="stack")
    stack.outputs.return_value = {
        "CognitoUserPoolTokenIssuerUrl": StackOutput(
            key="CognitoUserPoolTokenIssuerUrl",
            value=f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}",
        ),
    }
    appsync = MagicMock(name="appsync")
    appsync.graphql.return_value = {
        "createVirtualParticipant": {"id": "vp-1", "status": "INITIALIZING"},
    }
    client._stack_ops = stack
    client._appsync_ops = appsync
    client.vp._scheduler_arn = SFN_ARN
    return {"cognito": cognito, "secrets": secrets, "sfn": sfn, "appsync": appsync}


def _sfn_data(services):
    call = services["sfn"].start_sync_execution.call_args
    return json.loads(call.kwargs["input"])["data"]


def _vp_input(services):
    return services["appsync"].graphql.call_args.kwargs["variables"]["input"]


class TestLaunchWithoutUser:
    def test_sends_no_owner_and_empty_user_ids(self, client, services):
        client.vp.create(meeting_name="m", platform="TEAMS", meeting_id="1", wait=False)

        assert "owner" not in _vp_input(services)
        data = _sfn_data(services)
        assert data["userSub"] == ""
        assert data["userZoomSub"] == ""
        assert data["userName"] == "loadtest@lma"
        services["cognito"].admin_get_user.assert_not_called()

    def test_zoom_credentials_flag_requires_as_user(self, client, services):
        with pytest.raises(LMAVirtualParticipantError, match="requires as_user"):
            client.vp.create(
                meeting_name="m", platform="ZOOM", meeting_id="1",
                wait=False, use_zoom_credentials=True,
            )
        services["appsync"].graphql.assert_not_called()


class TestLaunchAsUser:
    def test_sets_owner_and_user_sub_like_the_ui(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()

        result = client.vp.create(
            meeting_name="m", platform="TEAMS", meeting_id="1",
            wait=False, as_user="alice@example.com",
        )

        services["cognito"].admin_get_user.assert_called_once_with(
            UserPoolId=POOL_ID, Username="alice@example.com",
        )
        assert _vp_input(services)["owner"] == "alice@example.com"
        data = _sfn_data(services)
        assert data["userSub"] == SUB
        assert data["userZoomSub"] == ""  # not a Zoom meeting
        assert data["userName"] == "alice@example.com"
        assert result.owner == "alice@example.com"
        assert result.user_sub == SUB
        assert result.zoom_credentials_used is False
        services["secrets"].describe_secret.assert_not_called()

    def test_owner_is_the_cognito_user_name_not_the_input(self, client, services):
        """A user found by email is owned under their Cognito user name."""
        services["cognito"].admin_get_user.side_effect = _client_error("UserNotFoundException")
        services["cognito"].list_users.return_value = {
            "Users": [{
                "Username": "alice",
                "Enabled": True,
                "Attributes": [
                    {"Name": "sub", "Value": SUB},
                    {"Name": "email", "Value": "Alice@Example.com"},
                ],
            }],
        }

        result = client.vp.create(
            meeting_name="m", platform="CHIME", meeting_id="1",
            wait=False, as_user="Alice@Example.com",
        )

        services["cognito"].list_users.assert_called_once_with(
            UserPoolId=POOL_ID, Filter='email = "Alice@Example.com"', Limit=2,
        )
        assert _vp_input(services)["owner"] == "alice"
        assert result.owner == "alice"

    def test_explicit_user_name_is_kept(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        client.vp.create(
            meeting_name="m", platform="TEAMS", meeting_id="1",
            wait=False, as_user="alice@example.com", user_name="Scribe",
        )
        assert _sfn_data(services)["userName"] == "Scribe"

    def test_zoom_uses_stored_credentials_when_present(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        services["secrets"].describe_secret.return_value = {"Name": "x"}

        result = client.vp.create(
            meeting_name="m", platform="ZOOM", meeting_id="1",
            wait=False, as_user="alice@example.com",
        )

        services["secrets"].describe_secret.assert_called_once_with(
            SecretId=f"test-lma-stack/zoom-credentials/{SUB}",
        )
        assert _sfn_data(services)["userZoomSub"] == SUB
        assert result.zoom_credentials_used is True

    def test_zoom_joins_unsigned_when_no_credentials_stored(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        services["secrets"].describe_secret.side_effect = _client_error("ResourceNotFoundException")

        result = client.vp.create(
            meeting_name="m", platform="ZOOM", meeting_id="1",
            wait=False, as_user="alice@example.com",
        )

        data = _sfn_data(services)
        assert data["userSub"] == SUB
        assert data["userZoomSub"] == ""
        assert result.zoom_credentials_used is False

    def test_zoom_secret_scheduled_for_deletion_counts_as_absent(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        services["secrets"].describe_secret.return_value = {"DeletedDate": "2026-01-01"}
        client.vp.create(
            meeting_name="m", platform="ZOOM", meeting_id="1",
            wait=False, as_user="alice@example.com",
        )
        assert _sfn_data(services)["userZoomSub"] == ""

    def test_zoom_check_denied_falls_back_unless_required(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        services["secrets"].describe_secret.side_effect = _client_error("AccessDeniedException")

        client.vp.create(
            meeting_name="m", platform="ZOOM", meeting_id="1",
            wait=False, as_user="alice@example.com",
        )
        assert _sfn_data(services)["userZoomSub"] == ""

        services["appsync"].graphql.reset_mock()
        with pytest.raises(LMAConfigurationError, match="secretsmanager:DescribeSecret"):
            client.vp.create(
                meeting_name="m", platform="ZOOM", meeting_id="1",
                wait=False, as_user="alice@example.com", use_zoom_credentials=True,
            )
        services["appsync"].graphql.assert_not_called()

    def test_required_zoom_credentials_missing_creates_no_row(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        services["secrets"].describe_secret.side_effect = _client_error("ResourceNotFoundException")

        with pytest.raises(LMAVirtualParticipantError, match="no stored Zoom credentials"):
            client.vp.create(
                meeting_name="m", platform="ZOOM", meeting_id="1",
                wait=False, as_user="alice@example.com", use_zoom_credentials=True,
            )
        services["appsync"].graphql.assert_not_called()

    def test_no_zoom_credentials_skips_the_check(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user()
        client.vp.create(
            meeting_name="m", platform="ZOOM", meeting_id="1",
            wait=False, as_user="alice@example.com", use_zoom_credentials=False,
        )
        services["secrets"].describe_secret.assert_not_called()
        data = _sfn_data(services)
        assert data["userSub"] == SUB
        assert data["userZoomSub"] == ""

    def test_zoom_credentials_rejected_for_other_platforms(self, client, services):
        with pytest.raises(LMAVirtualParticipantError, match="only applies to ZOOM"):
            client.vp.create(
                meeting_name="m", platform="TEAMS", meeting_id="1",
                wait=False, as_user="alice@example.com", use_zoom_credentials=True,
            )


class TestResolveUser:
    def test_unknown_user_creates_no_row(self, client, services):
        services["cognito"].admin_get_user.side_effect = _client_error("UserNotFoundException")
        services["cognito"].list_users.return_value = {"Users": []}

        with pytest.raises(LMAVirtualParticipantError, match="not found"):
            client.vp.create(
                meeting_name="m", platform="ZOOM", meeting_id="1",
                wait=False, as_user="nobody@example.com",
            )
        services["appsync"].graphql.assert_not_called()

    def test_disabled_user_rejected(self, client, services):
        services["cognito"].admin_get_user.return_value = _cognito_user(enabled=False)
        with pytest.raises(LMAVirtualParticipantError, match="disabled"):
            client.vp.resolve_user("alice@example.com")

    def test_ambiguous_email_rejected(self, client, services):
        services["cognito"].admin_get_user.side_effect = _client_error("UserNotFoundException")
        services["cognito"].list_users.return_value = {
            "Users": [
                {"Username": "a", "Attributes": [{"Name": "sub", "Value": "s1"}]},
                {"Username": "b", "Attributes": [{"Name": "sub", "Value": "s2"}]},
            ],
        }
        with pytest.raises(LMAVirtualParticipantError, match="More than one"):
            client.vp.resolve_user("shared@example.com")

    def test_quoted_identifier_never_reaches_the_email_filter(self, client, services):
        services["cognito"].admin_get_user.side_effect = _client_error("UserNotFoundException")
        with pytest.raises(LMAVirtualParticipantError, match="not found"):
            client.vp.resolve_user('x" or email ^= "')
        services["cognito"].list_users.assert_not_called()

    def test_lookup_denied_names_the_permission(self, client, services):
        services["cognito"].admin_get_user.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(LMAConfigurationError, match="cognito-idp:AdminGetUser"):
            client.vp.resolve_user("alice@example.com")

    def test_empty_identifier_rejected(self, client, services):
        with pytest.raises(LMAVirtualParticipantError, match="must not be empty"):
            client.vp.resolve_user("  ")


class TestExtractUserPoolId:
    def test_from_token_issuer_url(self):
        url = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"
        assert _extract_user_pool_id({"CognitoUserPoolTokenIssuerUrl": url}) == POOL_ID

    def test_from_local_ui_testing_env(self):
        env = f"VITE_USER_POOL_ID={POOL_ID} VITE_AWS_REGION=us-east-1"
        assert _extract_user_pool_id({"LocalUITestingEnv": env}) == POOL_ID

    def test_missing(self):
        assert _extract_user_pool_id({}) is None
