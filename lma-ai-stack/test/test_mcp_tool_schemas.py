# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The two MCP front doors offer the same tools, described alike (no AWS).

The API key endpoint lists every tool from ``MCP_TOOLS`` (index.py) and
``MEETING_TOOLS`` (tool_definitions.py). The OAuth endpoint, an AgentCore
Gateway target, declares its own copy of each tool in the stack template. A
client learns a tool's arguments only from that description, so the copies
name the same tools with the same descriptions and the same input schemas.

The gateway's schema format has Type, Description, Properties, Items and
Required only, so ``enum`` and ``additionalProperties`` are left out of the
comparison; the descriptions name the accepted values instead. Annotations are
the API key endpoint's alone.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
MCP_LAMBDA = AI_STACK / "source" / "lambda_functions" / "mcp_analytics"
ON_UPDATE_CALL = AI_STACK / "source" / "ui" / "src" / "graphql" / "queries" / "onUpdateCall.js"
GATEWAY_KEYS = {"Type", "Description", "Properties", "Items", "Required"}


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _text(value: str) -> str:
    return " ".join(value.split())


def _api_key_tools() -> list:
    tree = ast.parse((MCP_LAMBDA / "index.py").read_text(encoding="utf-8"))
    original = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "MCP_TOOLS" for t in node.targets)
    )
    spec = importlib.util.spec_from_file_location(
        "mcp_tool_definitions", MCP_LAMBDA / "tool_definitions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [*original, *module.MEETING_TOOLS]


def _gateway_tools() -> list:
    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]
    for resource in resources.values():
        config = (resource.get("Properties") or {}).get("TargetConfiguration") or {}
        payload = ((config.get("Mcp") or {}).get("Lambda") or {}).get("ToolSchema", {})
        if "InlinePayload" in payload:
            return payload["InlinePayload"]
    raise AssertionError("the gateway target has no tool schema")


def _as_gateway(schema: dict) -> dict:
    """An MCP input schema as the gateway's format writes it."""
    out = {"Type": schema["type"]}
    if "description" in schema:
        out["Description"] = _text(schema["description"])
    if "properties" in schema:
        out["Properties"] = {key: _as_gateway(value) for key, value in schema["properties"].items()}
    if "items" in schema:
        out["Items"] = _as_gateway(schema["items"])
    if schema.get("required"):
        out["Required"] = list(schema["required"])
    return out


def _normalized(schema: dict) -> dict:
    out = dict(schema)
    if "Description" in out:
        out["Description"] = _text(out["Description"])
    if "Properties" in out:
        out["Properties"] = {key: _normalized(value) for key, value in out["Properties"].items()}
    if "Items" in out:
        out["Items"] = _normalized(out["Items"])
    return out


def test_both_front_doors_offer_the_same_tools_in_the_same_order() -> None:
    assert [tool["Name"] for tool in _gateway_tools()] == [
        tool["name"] for tool in _api_key_tools()
    ]


@pytest.mark.parametrize("tool", _api_key_tools(), ids=lambda tool: tool["name"])
def test_each_tool_is_described_alike_on_both_front_doors(tool: dict) -> None:
    gateway = next(entry for entry in _gateway_tools() if entry["Name"] == tool["name"])
    assert _text(gateway["Description"]) == _text(tool["description"])
    assert _normalized(gateway["InputSchema"]) == _as_gateway(tool["inputSchema"])


def test_the_gateway_schema_uses_only_the_keys_its_format_has() -> None:
    def keys(schema: dict) -> set:
        found = set(schema)
        for value in (schema.get("Properties") or {}).values():
            found |= keys(value)
        if "Items" in schema:
            found |= keys(schema["Items"])
        return found

    for tool in _gateway_tools():
        assert keys(tool["InputSchema"]) <= GATEWAY_KEYS, tool["Name"]


@pytest.mark.parametrize("tool", _api_key_tools(), ids=lambda tool: tool["name"])
def test_each_tool_declares_all_three_hints(tool: dict) -> None:
    annotations = tool["annotations"]
    for hint in ("readOnlyHint", "destructiveHint", "idempotentHint"):
        assert isinstance(annotations.get(hint), bool), (tool["name"], hint)
    if annotations["readOnlyHint"]:
        assert annotations["destructiveHint"] is False, tool["name"]


def test_the_server_selects_what_the_meeting_page_subscribes_to() -> None:
    """Every Call mutation the MCP server makes publishes a whole onUpdateCall message."""
    ui = ON_UPDATE_CALL.read_text(encoding="utf-8")
    ui_selection = ui[ui.index("onUpdateCall {") + len("onUpdateCall {") : ui.rindex("}\n  }\n`")]
    source = (MCP_LAMBDA / "tools" / "call_fields.py").read_text(encoding="utf-8")
    server = re.search(r'SUBSCRIBED_CALL_FIELDS = """(.*?)"""', source, re.S).group(1)
    assert _text(server) == _text(ui_selection)
