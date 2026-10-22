# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the updateNovaSonicConfig resolver (no AWS calls)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("NOVA_SONIC_CONFIG_TABLE_NAME", "nova-sonic-config")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Loaded by path under a unique name: each config resolver's handler module is
# called `index.py`, so a plain `import index` would resolve to whichever one a
# shared test session imported first.
_SPEC = importlib.util.spec_from_file_location(
    "update_nova_sonic_config_resolver_index", Path(__file__).parent / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
with mock.patch("boto3.resource"):
    _SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}

# Attributes the resolver stamps on every write; the field-level assertions below
# are about the configuration itself, so they are dropped first.
AUDIT_ATTRIBUTES = ("UpdatedBy", "UpdatedAt")

CONFIG = {"systemPrompt": "Be brief", "promptMode": "inject", "meetingMode": "normal"}


def config_only(stored: dict) -> dict:
    return {key: value for key, value in stored.items() if key not in AUDIT_ATTRIBUTES}


def invoke(
    config: object,
    config_id: str = "CustomNovaSonicConfig",
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
                        "NovaSonicConfigId": config_id,
                        "ConfigData": json.dumps(config),
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
                        "NovaSonicConfigId": "CustomNovaSonicConfig",
                        "ConfigData": json.dumps(CONFIG),
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_known_fields_are_stored() -> None:
    response, stored = invoke(CONFIG)

    assert response == {"NovaSonicConfigId": "CustomNovaSonicConfig", "Success": True}
    assert config_only(stored) == {"NovaSonicConfigId": "CustomNovaSonicConfig", **CONFIG}


def test_fields_outside_the_allow_list_are_filtered_out() -> None:
    _, stored = invoke({"systemPrompt": "Be brief", "ExpiresAfter": 1, "Owner": "someone"})

    assert set(config_only(stored)) == {"NovaSonicConfigId", "systemPrompt"}


def test_every_specially_validated_field_is_also_on_the_allow_list() -> None:
    """A field validated but not allowed would be dropped before its check ran."""
    specialised = set(index.ENUM_FIELDS) | set(index.BOUNDED_TEXT_FIELDS)

    assert specialised <= index.ALLOWED_FIELDS, specialised - index.ALLOWED_FIELDS


def test_an_unrecognised_enum_value_is_dropped_rather_than_stored() -> None:
    _, stored = invoke({"systemPrompt": "Be brief", "promptMode": "not-a-mode"})

    assert set(config_only(stored)) == {"NovaSonicConfigId", "systemPrompt"}


def test_a_translator_label_longer_than_the_bound_is_trimmed_to_it() -> None:
    _, stored = invoke({"meetingMode": "translator", "translatorLanguageA": "x" * 500})

    assert stored["translatorLanguageA"] == "x" * index.MAX_LANGUAGE_LABEL_LEN


def test_a_translator_label_that_is_not_a_string_is_dropped() -> None:
    _, stored = invoke({"meetingMode": "translator", "translatorLanguageA": 17})

    assert "translatorLanguageA" not in stored


def test_only_the_custom_record_can_be_written() -> None:
    with pytest.raises(Exception, match="Only CustomNovaSonicConfig"):
        invoke(CONFIG, config_id="DefaultNovaSonicConfig")


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
                        "NovaSonicConfigId": "CustomNovaSonicConfig",
                        "ConfigData": "{not json",
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
    response, _ = invoke(CONFIG, identity=ADMIN_IDENTITY)

    assert response["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted() -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    response, _ = invoke(CONFIG, identity={"claims": {"cognito:groups": "Admin"}})

    assert response["Success"] is True


def test_a_comma_joined_group_claim_is_admitted() -> None:
    response, _ = invoke(CONFIG, identity={"claims": {"cognito:groups": "Users,Admin"}})

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


def test_a_refusal_is_logged_without_the_caller_s_email_address(
    capsys: pytest.CaptureFixture,
) -> None:
    """The log line identifies the caller by Cognito subject.

    An address in CloudWatch would outlive the decision it explains, on a
    retention period set deployment-wide. UpdatedBy on the stored item is the
    durable record of who changed a setting, and is where the address belongs.
    """
    refuse(
        {
            "claims": {
                "cognito:groups": ["Users"],
                "email": "someone@example.com",
                "cognito:username": "someone@example.com",
                "sub": "11111111-2222",
            }
        }
    )

    logged = capsys.readouterr().out
    assert "someone@example.com" not in logged
    assert "11111111-2222" in logged


def test_the_written_record_keeps_the_email_address(capsys: pytest.CaptureFixture) -> None:
    """The counterpart: the address is not dropped, only kept out of the log."""
    _, stored = invoke(CONFIG, identity=ADMIN_IDENTITY)

    assert stored["UpdatedBy"] == "admin@example.com"
    assert "admin@example.com" not in capsys.readouterr().out


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
                        "NovaSonicConfigId": "CustomNovaSonicConfig",
                        "ConfigData": json.dumps(CONFIG),
                        "cognito:groups": ["Admin"],
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it() -> None:
    _, stored = invoke(CONFIG)

    assert stored["UpdatedBy"] == "admin@example.com"
    assert stored["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present() -> None:
    _, stored = invoke(
        CONFIG, identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}}
    )

    assert stored["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request() -> None:
    _, stored = invoke({**CONFIG, "UpdatedBy": "someone-else@example.com"})

    assert stored["UpdatedBy"] == "admin@example.com"
