# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The troubleshooting agent's prompt is untouched by the configuration store.

Two changes meet at `LLMPromptTemplateTable`. The unified configuration store
moved the *summary* prompt templates out of it, into `ConfigurationTable`. The
troubleshooting agent keeps *its* system prompt in it, as the
`DefaultTroubleshootingAgentPrompt` / `CustomTroubleshootingAgentPrompt` pair,
seeded by the llm-template-setup stack's custom resource and read by
`troubleshooting_prompt.py`.

The decision recorded here is that the troubleshooting prompt stays where it
is for now: it is not migrated as a second section of the new store. So the
property to hold is that the migration leaves it reachable -- still seeded,
never written by the new seeder, and still resolved by the agent from the same
two keys -- and the failure it guards against is silent until somebody asks
the agent a question: the agent refuses to run without a usable prompt, by
design, so a table that stopped being seeded would surface as "the
troubleshooting agent is broken" rather than as anything about configuration.

It is asserted end to end, against one shared fake table, with the three real
modules: the legacy seeder writes, the new seeder runs over the same table
during an upgrade, and the agent's reader resolves. The template wiring each of
those relies on is asserted alongside.

No AWS calls.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

import pytest
import yaml
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[2]
LEGACY_SEEDER_DIR = REPO / "lma-llm-template-setup-stack" / "source" / "lambda_functions"
CONFIG_SEEDER_DIR = REPO / "lma-ai-stack" / "source" / "lambda_functions" / "configuration_seeder"
LAYER_DIR = REPO / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
AGENT_SOURCE_DIR = REPO / "lma-meetingassist-setup-stack" / "src"
PROMPT_FILE = LEGACY_SEEDER_DIR / "TroubleshootingAgentSystemPrompt.md"

LEGACY_TABLE = "llm-prompt-templates"
DEFAULT_KEY = "DefaultTroubleshootingAgentPrompt"
CUSTOM_KEY = "CustomTroubleshootingAgentPrompt"

for path in (str(LAYER_DIR), str(CONFIG_SEEDER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load(name: str, path: Path, stubs: Optional[Dict[str, Any]] = None) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs or {}), mock.patch("boto3.resource", mock.MagicMock()):
        spec.loader.exec_module(module)
    return module


LEGACY_SEEDER = _load(
    "llm_prompt_upload_for_migration_test",
    LEGACY_SEEDER_DIR / "llm_prompt_upload.py",
    {"cfnresponse": mock.MagicMock(SUCCESS="SUCCESS", FAILED="FAILED")},
)
CONFIG_SEEDER = _load(
    "configuration_seeder_for_migration_test", CONFIG_SEEDER_DIR / "lambda_function.py"
)
AGENT_PROMPT = _load(
    "troubleshooting_prompt_for_migration_test", AGENT_SOURCE_DIR / "troubleshooting_prompt.py"
)

from lma_config import codec  # noqa: E402  (layer on sys.path above)
from lma_config.store import ConfigurationStore  # noqa: E402

_SERIALIZE = TypeSerializer().serialize
_DESERIALIZE = TypeDeserializer().deserialize


def _refused() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "refused"}}, "PutItem"
    )


class LegacyTable:
    """`LLMPromptTemplateTable`, reachable both ways the three modules reach it.

    The legacy seeder writes through the boto3 resource API (plain values); the
    new seeder and the agent read through the low-level client (AttributeValue
    maps). One store behind both is what makes this an end-to-end check rather
    than three separate ones.
    """

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.writes = 0

    # resource-API side: what llm_prompt_upload.py calls
    def put_item(self, Item: Dict[str, Any], ConditionExpression: Optional[str] = None):  # noqa: N803
        key = Item["LLMPromptTemplateId"]
        if ConditionExpression and key in self.items:
            raise _refused()
        self.items[key] = dict(Item)
        self.writes += 1
        return {}

    # low-level side: what the configuration seeder and the agent call
    def client(self) -> Any:
        table = self

        class Client:
            def get_item(self, TableName: str, Key: Dict[str, Any], **_kwargs):  # noqa: N803
                assert TableName == LEGACY_TABLE
                item = table.items.get(Key["LLMPromptTemplateId"]["S"])
                if item is None:
                    return {}
                return {"Item": {name: _SERIALIZE(value) for name, value in item.items()}}

            def put_item(self, **_kwargs):
                raise AssertionError("nothing but the legacy seeder writes this table")

        return Client()


class ConfigurationTableFake:
    """`ConfigurationTable`, with the three conditions the store uses."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}

    def get_item(self, **request: Any) -> Dict[str, Any]:
        item = self.items.get(request["Key"][codec.KEY_ATTRIBUTE]["S"])
        return {"Item": item} if item is not None else {}

    def put_item(self, **request: Any) -> Dict[str, Any]:
        item = request["Item"]
        key = item[codec.KEY_ATTRIBUTE]["S"]
        condition = request.get("ConditionExpression")
        stored = self.items.get(key, {})
        if condition == "attribute_not_exists(#key)" and key in self.items:
            raise _refused()
        if condition == "attribute_not_exists(#version)" and "Version" in stored:
            raise _refused()
        if (
            condition == "#version = :expected"
            and stored.get("Version", {}).get("N")
            != (request["ExpressionAttributeValues"][":expected"]["N"])
        ):
            raise _refused()
        self.items[key] = item
        return {}


def run_legacy_seeder(table: LegacyTable, request_type: str) -> None:
    """The llm-template-setup stack's custom resource, against `table`."""
    resource = mock.MagicMock()
    resource.Table.return_value = table
    sent = []
    with (
        mock.patch.object(LEGACY_SEEDER.boto3, "resource", return_value=resource),
        mock.patch.dict(
            "os.environ", {"LAMBDA_TASK_ROOT": str(LEGACY_SEEDER_DIR), "DOMAIN": "Default"}
        ),
        mock.patch.object(
            LEGACY_SEEDER.cfnresponse, "send", lambda *args, **_kw: sent.append(args[2])
        ),
        mock.patch.object(LEGACY_SEEDER.botocore.exceptions, "ClientError", ClientError),
    ):
        LEGACY_SEEDER.lambda_handler(
            {
                "RequestType": request_type,
                "ResourceProperties": {"LLMPromptTemplateTableName": LEGACY_TABLE},
            },
            None,
        )
    assert sent == ["SUCCESS"], sent


def run_configuration_seeder(table: LegacyTable) -> Dict[str, str]:
    """The AI stack's configuration seeder, adopting from `table` on upgrade."""
    store = ConfigurationStore("lma-configuration", client=ConfigurationTableFake())
    return CONFIG_SEEDER.seed(
        store, domain="Default", legacy_table_name=LEGACY_TABLE, legacy_client=table.client()
    )


def snapshot(table: LegacyTable) -> Dict[str, Dict[str, Any]]:
    return {key: dict(item) for key, item in table.items.items()}


# ── end to end ──────────────────────────────────────────────────────────────


def test_the_prompt_still_seeds_and_still_resolves_after_the_migration() -> None:
    """Create, then the upgrade that brings the new store, then a diagnosis."""
    legacy = LegacyTable()
    run_legacy_seeder(legacy, "Create")
    assert {DEFAULT_KEY, CUSTOM_KEY} <= set(legacy.items)

    before = snapshot(legacy)
    writes_before = legacy.writes
    run_configuration_seeder(legacy)

    # The new seeder read the summary prompts it adopts and wrote nothing here.
    assert legacy.writes == writes_before
    assert snapshot(legacy) == before

    prompt = AGENT_PROMPT.get_system_prompt(legacy.client(), LEGACY_TABLE)
    assert prompt == PROMPT_FILE.read_text().strip()


def test_an_operators_tuned_prompt_survives_the_migrating_upgrade() -> None:
    """The override is what makes the prompt worth keeping in a table at all."""
    legacy = LegacyTable()
    run_legacy_seeder(legacy, "Create")
    tuned = "You are diagnosing this specific deployment. " * 10
    legacy.items[CUSTOM_KEY]["systemPrompt"] = tuned

    # The upgrade: both seeders run, the legacy one as an Update.
    run_legacy_seeder(legacy, "Update")
    run_configuration_seeder(legacy)

    assert AGENT_PROMPT.get_system_prompt(legacy.client(), LEGACY_TABLE) == tuned.strip()


def test_the_troubleshooting_prompt_is_not_adopted_into_the_configuration_profile() -> None:
    """Adoption takes the summary prompts and nothing else from that table.

    Adopting the troubleshooting prompt as well would leave two copies with no
    rule for which one wins -- the agent reads the old table, an editor would
    write the new one.
    """
    legacy = LegacyTable()
    run_legacy_seeder(legacy, "Create")
    legacy.items["CustomSummaryPromptTemplates"]["1#SUMMARY"] = "an edited summary prompt"

    adopted = CONFIG_SEEDER.adopt_legacy_prompts(
        LEGACY_TABLE, {"1#SUMMARY": "the shipped one"}, client=legacy.client()
    )
    assert adopted == {"summary_prompt_templates": {"1#SUMMARY": "an edited summary prompt"}}
    flat = str(adopted)
    assert "systemPrompt" not in flat and "Troubleshooting" not in flat


# ── the wiring each of those relies on ──────────────────────────────────────


class _CfnLoader(yaml.SafeLoader):
    """Tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def main_resources() -> Dict[str, Any]:
    return yaml.load((REPO / "lma-main.yaml").read_text(), Loader=_CfnLoader)["Resources"]


def test_the_agent_is_still_given_the_table_the_legacy_stack_seeds(main_resources) -> None:
    """Not the configuration table: the agent reads its prompt from the old one."""
    assert "LLMTEMPLATESTACK" in main_resources, "the stack that seeds the prompt is gone"
    wired = [
        name
        for name, resource in main_resources.items()
        if resource.get("Type") == "AWS::CloudFormation::Stack"
        and resource.get("Properties", {}).get("Parameters", {}).get("LLMPromptTemplateTableName")
        == {"Fn::GetAtt": "LLMTEMPLATESTACK.Outputs.LLMPromptTemplateTableName"}
    ]
    # The AI stack (whose configuration seeder adopts from it) and the
    # meeting-assist stack (whose troubleshooting function reads its prompt).
    assert len(wired) >= 2, f"only {wired} are given the legacy prompt table"


def test_the_legacy_seeder_still_reruns_when_the_prompt_file_changes() -> None:
    """The publish step hashes the directory the prompt file lives in."""
    sys.path.insert(0, str(REPO / "lib" / "lma_sdk"))
    from lma_sdk._core.publish import STACK_DEFINITIONS  # noqa: PLC0415

    (definition,) = [d for d in STACK_DEFINITIONS if d.name == "lma-llm-template-setup-stack"]
    deployment = REPO / definition.name / (definition.deployment_subdir or "deployment")
    hashed = (deployment / definition.hash_source_dir).resolve()
    assert PROMPT_FILE.resolve().is_relative_to(hashed)
    template = (deployment / definition.template_file).read_text()
    assert f"{definition.hash_template_field}:" in template


def test_the_configuration_seeder_can_only_read_the_legacy_table() -> None:
    """GetItem alone, so the migration cannot write over the agent's prompt."""
    resources = yaml.load(
        (REPO / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml").read_text(),
        Loader=_CfnLoader,
    )["Resources"]
    statements = [
        statement
        for policy in resources["ConfigurationSeederRole"]["Properties"]["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    on_legacy = [s for s in statements if "LLMPromptTemplateTableName" in str(s.get("Resource"))]
    assert on_legacy, "no statement names the legacy table, so this test checks nothing"
    for statement in on_legacy:
        actions = (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        assert actions == ["dynamodb:GetItem"], actions
