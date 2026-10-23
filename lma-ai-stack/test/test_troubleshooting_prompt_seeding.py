# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The prompt-seeding custom resource's Default/Custom semantics (no AWS).

Lives here rather than beside the seeder because `make test-lambdas` discovers
tests only under the AI stack's lambda directories, the shared layer, the ASR
stack and the meeting-assist source. This is the same route the existing
cross-stack suites in this directory take: load the handler by path, stub what
it builds at import, and assert the behaviour.

What is asserted is the one thing that silently destroys an operator's work:
the default item must be rewritten on every stack update, and the custom item
must be written only on create. Getting that backwards means an upgrade
discards a tuned prompt, and the tuning is the whole reason the prompt is in
DynamoDB.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
SEEDER_DIR = REPO / "lma-llm-template-setup-stack" / "source" / "lambda_functions"
PROMPT_FILE = SEEDER_DIR / "TroubleshootingAgentSystemPrompt.md"


def load_seeder() -> Any:
    """Import llm_prompt_upload.py by path with boto3 and cfnresponse stubbed."""
    spec = importlib.util.spec_from_file_location(
        "llm_prompt_upload_under_test", SEEDER_DIR / "llm_prompt_upload.py"
    )
    module = importlib.util.module_from_spec(spec)
    # cfnresponse ships beside the handler but imports nothing useful here, and
    # boto3 must not build a client at import time.
    with mock.patch.dict(
        sys.modules, {"cfnresponse": mock.MagicMock(SUCCESS="SUCCESS", FAILED="FAILED")}
    ), mock.patch("boto3.resource", mock.MagicMock()):
        spec.loader.exec_module(module)
    return module


SEEDER = load_seeder()


class ConditionalCheckFailed(Exception):
    """Stands in for botocore's ConditionalCheckFailedException."""

    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Records writes, and can refuse a conditional one as DynamoDB would.

    `existing` names the partition keys already present, so a conditional
    `put_item` on one of them fails the way the real table would.
    """

    def __init__(self, existing: Optional[set[str]] = None) -> None:
        self.written: List[Dict[str, Any]] = []
        self.conditional_keys: List[str] = []
        self.existing = existing or set()

    def put_item(
        self, Item: Dict[str, Any], ConditionExpression: Optional[str] = None  # noqa: N803
    ) -> Dict[str, str]:
        key = Item["LLMPromptTemplateId"]
        if ConditionExpression:
            self.conditional_keys.append(key)
            if key in self.existing:
                raise ConditionalCheckFailed()
        self.written.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": "200"}}


def invoke(
    request_type: str, existing: Optional[set[str]] = None
) -> List[Dict[str, Any]]:
    """Run the custom resource for one request type; return the items written."""
    table = FakeTable(existing)
    resource = mock.MagicMock()
    resource.Table.return_value = table
    responses: List[Any] = []

    with mock.patch.object(SEEDER.boto3, "resource", return_value=resource), mock.patch.dict(
        "os.environ", {"LAMBDA_TASK_ROOT": str(SEEDER_DIR), "DOMAIN": "Default"}
    ), mock.patch.object(
        SEEDER.cfnresponse, "send", lambda *args, **kwargs: responses.append(args)
    ), mock.patch.object(
        SEEDER.botocore.exceptions, "ClientError", ConditionalCheckFailed
    ):
        SEEDER.lambda_handler(
            {
                "RequestType": request_type,
                "ResourceProperties": {"LLMPromptTemplateTableName": "prompt-table"},
            },
            None,
        )

    assert responses, "the custom resource must always respond to CloudFormation"
    assert responses[0][2] == SEEDER.cfnresponse.SUCCESS, responses
    return table.written


def written_keys(items: List[Dict[str, Any]]) -> List[str]:
    return [item["LLMPromptTemplateId"] for item in items]


def item_for(items: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    matching = [i for i in items if i["LLMPromptTemplateId"] == key]
    assert len(matching) == 1, f"{key} written {len(matching)} times"
    return matching[0]


# ── create ──────────────────────────────────────────────────────────────────


def test_a_create_seeds_both_troubleshooting_items() -> None:
    keys = written_keys(invoke("Create"))

    assert SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK in keys
    assert SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK in keys


def test_the_default_item_carries_the_prompt_from_the_file() -> None:
    item = item_for(invoke("Create"), SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK)

    assert item["systemPrompt"] == PROMPT_FILE.read_text(encoding="utf-8")


def test_the_prompt_is_stored_with_its_line_breaks_intact() -> None:
    """The summary templates in this table encode newlines as '<br>' because
    they are edited in the console. A 200-line system prompt stored that way
    would not be reviewable in a pull request, so it is written verbatim.
    """
    item = item_for(invoke("Create"), SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK)

    assert "\n" in item["systemPrompt"]
    assert "<br>" not in item["systemPrompt"]


def test_the_custom_item_is_seeded_without_a_prompt() -> None:
    """Seeding it with a copy of the default would make every later upgrade
    look like an operator override and permanently freeze the prompt."""
    item = item_for(invoke("Create"), SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK)

    assert "systemPrompt" not in item


# ── the custom item reaches upgraded stacks too ─────────────────────────────


def test_an_update_seeds_the_custom_item_when_it_is_absent() -> None:
    """A stack created before this release has no custom item, and the
    deployment's console link for editing the prompt would open an item that is
    not there. Seeding only on Create would leave every upgraded stack so.
    """
    item = item_for(invoke("Update"), SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK)

    assert "systemPrompt" not in item
    assert "no redeployment is needed" in item["*Information*"]


def test_an_existing_custom_item_is_never_overwritten() -> None:
    """This is what makes seeding on Update safe. An operator's tuned prompt
    surviving an upgrade is the whole reason the prompt lives in DynamoDB.
    """
    written = invoke("Update", existing={SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK})

    assert SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK not in written_keys(written)


def test_the_custom_item_is_written_only_under_a_condition() -> None:
    """An unconditional write on Update would discard a tuned prompt, so the
    condition is not an optimisation -- it is the safety property."""
    table = FakeTable()
    resource = mock.MagicMock()
    resource.Table.return_value = table
    with mock.patch.object(
        SEEDER.boto3, "resource", return_value=resource
    ), mock.patch.dict(
        "os.environ", {"LAMBDA_TASK_ROOT": str(SEEDER_DIR), "DOMAIN": "Default"}
    ), mock.patch.object(SEEDER.cfnresponse, "send", lambda *a, **k: None):
        SEEDER.lambda_handler(
            {
                "RequestType": "Update",
                "ResourceProperties": {"LLMPromptTemplateTableName": "prompt-table"},
            },
            None,
        )

    assert table.conditional_keys == [SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK]


def test_a_conditional_failure_is_not_reported_as_a_stack_failure() -> None:
    """An already-present custom item is the normal case on every upgrade after
    the first; failing the custom resource for it would fail the deployment."""
    table = FakeTable(existing={SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK})
    resource = mock.MagicMock()
    resource.Table.return_value = table
    responses: List[Any] = []
    with mock.patch.object(
        SEEDER.boto3, "resource", return_value=resource
    ), mock.patch.dict(
        "os.environ", {"LAMBDA_TASK_ROOT": str(SEEDER_DIR), "DOMAIN": "Default"}
    ), mock.patch.object(
        SEEDER.cfnresponse, "send", lambda *args, **kwargs: responses.append(args)
    ), mock.patch.object(
        SEEDER.botocore.exceptions, "ClientError", ConditionalCheckFailed
    ):
        SEEDER.lambda_handler(
            {
                "RequestType": "Update",
                "ResourceProperties": {"LLMPromptTemplateTableName": "prompt-table"},
            },
            None,
        )

    assert responses[0][2] == SEEDER.cfnresponse.SUCCESS


def test_any_other_write_failure_still_fails_the_stack() -> None:
    """Only the conditional check is tolerated; a throttle or a denial must not
    be swallowed into a deployment that looks successful."""

    class Exploding(FakeTable):
        def put_item(self, Item, ConditionExpression=None):  # noqa: N803
            if ConditionExpression:
                raise ConditionalCheckFailedWithOtherCode()
            return super().put_item(Item)

    class ConditionalCheckFailedWithOtherCode(Exception):
        response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}

    table = Exploding()
    resource = mock.MagicMock()
    resource.Table.return_value = table
    responses: List[Any] = []
    with mock.patch.object(
        SEEDER.boto3, "resource", return_value=resource
    ), mock.patch.dict(
        "os.environ", {"LAMBDA_TASK_ROOT": str(SEEDER_DIR), "DOMAIN": "Default"}
    ), mock.patch.object(
        SEEDER.cfnresponse, "send", lambda *args, **kwargs: responses.append(args)
    ), mock.patch.object(
        SEEDER.botocore.exceptions, "ClientError", ConditionalCheckFailedWithOtherCode
    ):
        SEEDER.lambda_handler(
            {
                "RequestType": "Update",
                "ResourceProperties": {"LLMPromptTemplateTableName": "prompt-table"},
            },
            None,
        )

    assert responses[0][2] == SEEDER.cfnresponse.FAILED


def test_the_custom_item_documents_how_to_override() -> None:
    """Its '*Information*' attribute is the only in-product documentation an
    operator who finds this table will see."""
    item = item_for(invoke("Create"), SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK)

    assert "systemPrompt" in item["*Information*"]
    assert "no redeployment is needed" in item["*Information*"]


def test_the_default_item_warns_that_edits_to_it_are_lost() -> None:
    item = item_for(invoke("Create"), SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK)

    assert "Do not edit" in item["*Information*"]
    assert SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK in item["*Information*"]


# ── update ──────────────────────────────────────────────────────────────────


def test_an_update_rewrites_the_default_so_a_new_prompt_ships() -> None:
    """A prompt improvement that did not reach a deployed stack would make
    every change to this file cosmetic."""
    item = item_for(invoke("Update"), SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK)

    assert item["systemPrompt"] == PROMPT_FILE.read_text(encoding="utf-8")


def test_a_delete_writes_nothing() -> None:
    assert invoke("Delete") == []


# ── the existing summary prompts are unaffected ─────────────────────────────


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_the_summary_prompt_items_are_still_seeded(request_type: str) -> None:
    keys = written_keys(invoke(request_type))

    assert SEEDER.DEFAULT_PROMPT_TEMPLATES_PK in keys


def test_the_custom_summary_prompts_are_still_create_only() -> None:
    assert SEEDER.CUSTOM_PROMPT_TEMPLATES_PK in written_keys(invoke("Create"))
    assert SEEDER.CUSTOM_PROMPT_TEMPLATES_PK not in written_keys(invoke("Update"))


def test_the_four_item_keys_are_distinct() -> None:
    """A collision would have one pair of items silently overwrite the other."""
    keys = [
        SEEDER.DEFAULT_PROMPT_TEMPLATES_PK,
        SEEDER.CUSTOM_PROMPT_TEMPLATES_PK,
        SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK,
        SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK,
    ]

    assert len(set(keys)) == 4


def test_the_seeder_and_the_agent_agree_on_the_item_keys() -> None:
    """The seeder writes these and the agent reads them; a rename on one side
    only would leave the agent refusing to run with no prompt in the table.
    """
    sys.path.insert(0, str(REPO / "lma-meetingassist-setup-stack" / "src"))
    try:
        import troubleshooting_prompt
    finally:
        sys.path.pop(0)

    assert troubleshooting_prompt.DEFAULT_PROMPT_PK == (
        SEEDER.DEFAULT_TROUBLESHOOTING_PROMPT_PK
    )
    assert troubleshooting_prompt.CUSTOM_PROMPT_PK == (
        SEEDER.CUSTOM_TROUBLESHOOTING_PROMPT_PK
    )
    assert troubleshooting_prompt.PROMPT_ATTRIBUTE == "systemPrompt"


def test_the_shipped_prompt_is_long_enough_for_the_agent_to_accept_it() -> None:
    """The reader rejects a short value as a fragment rather than a prompt."""
    sys.path.insert(0, str(REPO / "lma-meetingassist-setup-stack" / "src"))
    try:
        import troubleshooting_prompt
    finally:
        sys.path.pop(0)

    text = PROMPT_FILE.read_text(encoding="utf-8")

    assert len(text) > troubleshooting_prompt._MINIMUM_USEFUL_LENGTH
