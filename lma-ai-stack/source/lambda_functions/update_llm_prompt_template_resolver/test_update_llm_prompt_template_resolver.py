# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the updateLLMPromptTemplate resolver (no AWS calls)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("LLM_PROMPT_TEMPLATE_TABLE_NAME", "llm-prompt-templates")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Loaded by path under a unique name: each config resolver's handler module is
# called `index.py`, so a plain `import index` would resolve to whichever one a
# shared test session imported first.
_SPEC = importlib.util.spec_from_file_location(
    "update_llm_prompt_template_resolver_index", Path(__file__).parent / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
with mock.patch("boto3.resource"):
    _SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}

# Attributes the resolver stamps on every write; the field-level assertions below
# are about the configuration itself, so they are dropped first.
AUDIT_ATTRIBUTES = ("UpdatedBy", "UpdatedAt")

TEMPLATES = {"1#SUMMARY": "Summarize {transcript}", "2#ACTIONS": "List actions in {transcript}"}


def config_only(stored: dict) -> dict:
    return {key: value for key, value in stored.items() if key not in AUDIT_ATTRIBUTES}


def invoke(
    config: object,
    config_id: str = "CustomSummaryPromptTemplates",
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
                        "LLMPromptTemplateId": config_id,
                        "TemplateConfig": json.dumps(config),
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
                        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
                        "TemplateConfig": json.dumps(TEMPLATES),
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_templates_are_stored_under_their_sequence_keys() -> None:
    response, stored = invoke(TEMPLATES)

    assert response == {
        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
        "Success": True,
    }
    assert config_only(stored) == {
        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
        **TEMPLATES,
    }


def test_keys_outside_the_sequence_form_are_filtered_out() -> None:
    _, stored = invoke({"1#SUMMARY": "Summarize", "ExpiresAfter": 1, "Owner": "someone"})

    assert set(config_only(stored)) == {"LLMPromptTemplateId", "1#SUMMARY"}


def test_an_empty_configuration_clears_the_custom_templates() -> None:
    """Resetting to defaults stores the key alone, so the reader falls back."""
    _, stored = invoke({})

    assert set(config_only(stored)) == {"LLMPromptTemplateId"}


def test_only_the_custom_record_can_be_written() -> None:
    with pytest.raises(Exception, match="Only CustomSummaryPromptTemplates"):
        invoke(TEMPLATES, config_id="DefaultSummaryPromptTemplates")


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
                        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
                        "TemplateConfig": "{not json",
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
    response, _ = invoke(TEMPLATES, identity=ADMIN_IDENTITY)

    assert response["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted() -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    response, _ = invoke(TEMPLATES, identity={"claims": {"cognito:groups": "Admin"}})

    assert response["Success"] is True


def test_a_comma_joined_group_claim_is_admitted() -> None:
    response, _ = invoke(TEMPLATES, identity={"claims": {"cognito:groups": "Users,Admin"}})

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
    _, stored = invoke(TEMPLATES, identity=ADMIN_IDENTITY)

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
                        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
                        "TemplateConfig": json.dumps(TEMPLATES),
                        "cognito:groups": ["Admin"],
                    }
                },
            },
            None,
        )
    table.put_item.assert_not_called()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it() -> None:
    _, stored = invoke(TEMPLATES)

    assert stored["UpdatedBy"] == "admin@example.com"
    assert stored["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present() -> None:
    _, stored = invoke(
        TEMPLATES, identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}}
    )

    assert stored["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request() -> None:
    _, stored = invoke({**TEMPLATES, "UpdatedBy": "someone-else@example.com"})

    assert stored["UpdatedBy"] == "admin@example.com"
