# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the updateAsrConfig resolver (no AWS calls)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("ASR_CONFIG_TABLE_NAME", "asr-config")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Loaded by path under a unique name: each config resolver's handler module is
# called `index.py`, so a plain `import index` would resolve to whichever one a
# shared test session imported first.
_SPEC = importlib.util.spec_from_file_location(
    "update_asr_config_resolver_index", Path(__file__).parent / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
with mock.patch("boto3.resource"):
    _SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}

# Attributes the resolver stamps on every write; the field-level assertions below
# are about the configuration itself, so they are dropped first.
AUDIT_ATTRIBUTES = ("UpdatedBy", "UpdatedAt")


def config_only(stored: dict) -> dict:
    return {key: value for key, value in stored.items() if key not in AUDIT_ATTRIBUTES}


def invoke(
    config: object,
    config_id: str = "CustomAsrConfig",
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
                    "input": {"AsrConfigId": config_id, "ConfigData": json.dumps(config)}
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
                        "AsrConfigId": "CustomAsrConfig",
                        "ConfigData": json.dumps({"streamingEngineMicrovm": True}),
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


def test_the_switches_are_stored_as_booleans() -> None:
    response, stored = invoke(
        {
            "streamingEngineMicrovm": True,
            "virtualParticipantEngineMicrovm": False,
            "diarizeVirtualParticipant": False,
        }
    )

    assert response == {"AsrConfigId": "CustomAsrConfig", "Success": True}
    assert config_only(stored) == {
        "AsrConfigId": "CustomAsrConfig",
        "streamingEngineMicrovm": True,
        "virtualParticipantEngineMicrovm": False,
        "diarizeVirtualParticipant": False,
    }


def test_string_booleans_from_an_older_client_are_parsed_not_truth_tested() -> None:
    # bool("false") is True.
    _, stored = invoke(
        {
            "streamingEngineMicrovm": "true",
            "virtualParticipantEngineMicrovm": "false",
            "diarizeVirtualParticipant": "FALSE",
        }
    )

    assert stored["streamingEngineMicrovm"] is True
    assert stored["virtualParticipantEngineMicrovm"] is False
    assert stored["diarizeVirtualParticipant"] is False


def test_a_value_that_is_not_a_boolean_is_dropped_rather_than_coerced() -> None:
    _, stored = invoke(
        {
            "streamingEngineMicrovm": "yes",
            "virtualParticipantEngineMicrovm": 1,
            "diarizeVirtualParticipant": None,
        }
    )

    assert config_only(stored) == {"AsrConfigId": "CustomAsrConfig"}


def test_retired_tuning_fields_are_filtered_out() -> None:
    """The threshold and its friends live in the image now; a stale client that
    still sends them must not be able to put a number back into the table."""
    _, stored = invoke(
        {
            "streamingEngineMicrovm": True,
            "engineDefaultMicrovm": True,
            "speakerThreshold": 0.3,
            "minSegmentMs": 2500,
            "maxSpeakers": 4,
            "liveTurnCut": False,
            "modelId": "../../etc/passwd",
        }
    )

    assert set(config_only(stored)) == {"AsrConfigId", "streamingEngineMicrovm"}


def test_only_the_custom_record_can_be_written() -> None:
    with pytest.raises(Exception, match="Only CustomAsrConfig"):
        invoke({"streamingEngineMicrovm": True}, config_id="DefaultAsrConfig")


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
                    "input": {"AsrConfigId": "CustomAsrConfig", "ConfigData": "{not json"}
                },
            },
            None,
        )
    table.put_item.assert_not_called()


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        invoke([1, 2, 3])


def test_the_allow_list_is_exactly_the_three_switches() -> None:
    """Adding a field here means adding it to the AppSync type, the transcriber's
    and the VP's readers and the Transcription Engine page - fail loudly if it drifts."""
    assert index.ALLOWED_FIELDS == {
        "streamingEngineMicrovm",
        "virtualParticipantEngineMicrovm",
        "diarizeVirtualParticipant",
    }


# ── group membership ────────────────────────────────────────────────────────


def test_a_member_of_the_admin_group_is_admitted() -> None:
    response, _ = invoke({"streamingEngineMicrovm": True}, identity=ADMIN_IDENTITY)

    assert response == {"AsrConfigId": "CustomAsrConfig", "Success": True}


def test_a_single_group_claim_sent_as_a_string_is_admitted() -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    response, _ = invoke(
        {"streamingEngineMicrovm": True},
        identity={"claims": {"cognito:groups": "Admin", "email": "admin@example.com"}},
    )

    assert response["Success"] is True


def test_a_comma_joined_group_claim_is_admitted() -> None:
    response, _ = invoke(
        {"streamingEngineMicrovm": True},
        identity={"claims": {"cognito:groups": "Users,Admin"}},
    )

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
    _, stored = invoke({"streamingEngineMicrovm": True}, identity=ADMIN_IDENTITY)

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
                        "AsrConfigId": "CustomAsrConfig",
                        "ConfigData": json.dumps({"streamingEngineMicrovm": True}),
                        "cognito:groups": ["Admin"],
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it() -> None:
    _, stored = invoke({"streamingEngineMicrovm": True})

    assert stored["UpdatedBy"] == "admin@example.com"
    assert stored["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present() -> None:
    _, stored = invoke(
        {"streamingEngineMicrovm": True},
        identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}},
    )

    assert stored["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request() -> None:
    _, stored = invoke({"streamingEngineMicrovm": True, "UpdatedBy": "someone-else@example.com"})

    assert stored["UpdatedBy"] == "admin@example.com"
