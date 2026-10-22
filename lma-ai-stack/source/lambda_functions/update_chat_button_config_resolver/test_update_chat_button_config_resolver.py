# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the updateChatButtonConfig resolver (no AWS calls)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("CHAT_BUTTON_CONFIG_TABLE_NAME", "chat-button-config")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Loaded by path under a unique name: each config resolver's handler module is
# called `index.py`, so a plain `import index` would resolve to whichever one a
# shared test session imported first.
_SPEC = importlib.util.spec_from_file_location(
    "update_chat_button_config_resolver_index", Path(__file__).parent / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
with mock.patch("boto3.resource"):
    _SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}

# Attributes the resolver stamps on every write; the field-level assertions below
# are about the configuration itself, so they are dropped first.
AUDIT_ATTRIBUTES = ("UpdatedBy", "UpdatedAt")

BUTTONS = {"1#Action Items": "List the action items", "2#Summary": "Summarize the meeting"}


def config_only(stored: dict) -> dict:
    return {key: value for key, value in stored.items() if key not in AUDIT_ATTRIBUTES}


def invoke(
    config: object,
    config_id: str = "CustomChatButtonConfig",
    identity: object = ADMIN_IDENTITY,
) -> tuple[dict, dict]:
    """Run the resolver against a fake table; returns (response, stored item)."""
    stored: dict = {}
    table = mock.Mock()
    table.put_item.side_effect = lambda Item: stored.update(Item)  # noqa: N803
    with mock.patch.object(index.dynamodb, "Table", return_value=table):
        response = index.lambda_handler(
            {
                "identity": identity,
                "arguments": {
                    "input": {
                        "ChatButtonConfigId": config_id,
                        "ButtonConfig": json.dumps(config),
                    }
                },
            },
            None,
        )
    return response, stored


def refuse(identity: object) -> None:
    """Assert the resolver refuses `identity` and writes nothing."""
    table = mock.Mock()
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=table),
        pytest.raises(Exception, match="Unauthorized"),
    ):
        index.lambda_handler(
            {
                "identity": identity,
                "arguments": {
                    "input": {
                        "ChatButtonConfigId": "CustomChatButtonConfig",
                        "ButtonConfig": json.dumps(BUTTONS),
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_buttons_are_stored_under_their_sequence_keys() -> None:
    response, stored = invoke(BUTTONS)

    assert response == {"ChatButtonConfigId": "CustomChatButtonConfig", "Success": True}
    assert config_only(stored) == {"ChatButtonConfigId": "CustomChatButtonConfig", **BUTTONS}


def test_keys_outside_the_sequence_form_are_filtered_out() -> None:
    _, stored = invoke({"1#Summary": "Summarize", "ExpiresAfter": 1, "Owner": "someone"})

    assert set(config_only(stored)) == {"ChatButtonConfigId", "1#Summary"}


def test_an_empty_configuration_clears_the_custom_buttons() -> None:
    """Resetting to defaults stores the key alone, so the reader falls back."""
    _, stored = invoke({})

    assert set(config_only(stored)) == {"ChatButtonConfigId"}


def test_only_the_custom_record_can_be_written() -> None:
    with pytest.raises(Exception, match="Only CustomChatButtonConfig"):
        invoke(BUTTONS, config_id="DefaultChatButtonConfig")


def test_an_arbitrary_partition_key_is_refused() -> None:
    table = mock.Mock()
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=table),
        pytest.raises(Exception, match="Only CustomChatButtonConfig"),
    ):
        index.lambda_handler(
            {
                "identity": ADMIN_IDENTITY,
                "arguments": {
                    "input": {
                        "ChatButtonConfigId": "SomeOtherRecord",
                        "ButtonConfig": json.dumps(BUTTONS),
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


def test_malformed_json_is_rejected() -> None:
    table = mock.Mock()
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=table),
        pytest.raises(Exception, match="Invalid JSON"),
    ):
        index.lambda_handler(
            {
                "identity": ADMIN_IDENTITY,
                "arguments": {
                    "input": {
                        "ChatButtonConfigId": "CustomChatButtonConfig",
                        "ButtonConfig": "{not json",
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        invoke([1, 2, 3])


# ── group membership ────────────────────────────────────────────────────────


def test_a_member_of_the_admin_group_is_admitted() -> None:
    response, _ = invoke(BUTTONS, identity=ADMIN_IDENTITY)

    assert response["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted() -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    response, _ = invoke(BUTTONS, identity={"claims": {"cognito:groups": "Admin"}})

    assert response["Success"] is True


def test_a_comma_joined_group_claim_is_admitted() -> None:
    response, _ = invoke(BUTTONS, identity={"claims": {"cognito:groups": "Users,Admin"}})

    assert response["Success"] is True


@pytest.mark.parametrize(
    "identity",
    [
        {"claims": {"cognito:groups": ["Users"]}},
        {"claims": {"cognito:groups": []}},
        {"claims": {"cognito:groups": "users"}},
        {"claims": {"cognito:groups": "SuperAdmins"}},
    ],
)
def test_a_caller_outside_the_admin_group_is_refused(identity: dict) -> None:
    refuse(identity)


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"claims": {}},
        {"claims": None},
        {"claims": {"sub": "u1"}},
        {"claims": {"cognito:groups": 17}},
        None,
    ],
)
def test_a_request_with_no_readable_group_claim_is_refused(identity: object) -> None:
    """Absent or unreadable group membership is refused, not treated as unrestricted."""
    refuse(identity)


def test_group_membership_in_the_arguments_does_not_admit_a_caller() -> None:
    """Only the identity block is consulted, so a claim in the input carries nothing."""
    table = mock.Mock()
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=table),
        pytest.raises(Exception, match="Unauthorized"),
    ):
        index.lambda_handler(
            {
                "identity": {"claims": {"cognito:groups": ["Users"]}},
                "arguments": {
                    "input": {
                        "ChatButtonConfigId": "CustomChatButtonConfig",
                        "ButtonConfig": json.dumps(BUTTONS),
                        "cognito:groups": ["Admin"],
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it() -> None:
    _, stored = invoke(BUTTONS)

    assert stored["UpdatedBy"] == "admin@example.com"
    assert stored["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present() -> None:
    _, stored = invoke(
        BUTTONS, identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}}
    )

    assert stored["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request() -> None:
    _, stored = invoke({**BUTTONS, "UpdatedBy": "someone-else@example.com"})

    assert stored["UpdatedBy"] == "admin@example.com"
