# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""list_active_meetings is offered by the API key endpoint only (no AWS).

The OAuth endpoint (an AgentCore Gateway target) lists the tools in its own copy
of the tool schemas in the stack template, and the function picks a tool there by
the shape of its input. The tool takes only an optional ``limit``, which is the
shape of list_meetings, so it is neither listed in the template nor chosen by
that shape.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
MCP_LAMBDA = AI_STACK / "source" / "lambda_functions" / "mcp_analytics"
TOOL = "list_active_meetings"


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


def _gateway_tool_names() -> set:
    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]
    for resource in resources.values():
        config = (resource.get("Properties") or {}).get("TargetConfiguration") or {}
        payload = ((config.get("Mcp") or {}).get("Lambda") or {}).get("ToolSchema", {})
        if "InlinePayload" in payload:
            return {tool["Name"] for tool in payload["InlinePayload"]}
    raise AssertionError("no gateway target with an inline tool schema")


def _index_tree() -> ast.Module:
    return ast.parse((MCP_LAMBDA / "index.py").read_text(encoding="utf-8"))


def _api_key_tools() -> dict:
    for node in _index_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MCP_TOOLS" for t in node.targets
        ):
            return {tool["name"]: tool for tool in ast.literal_eval(node.value)}
    raise AssertionError("MCP_TOOLS not found")


def test_the_api_key_endpoint_offers_the_tool_as_read_only() -> None:
    tool = _api_key_tools()[TOOL]
    assert tool["annotations"] == {"readOnlyHint": True}
    assert set(tool["inputSchema"]["properties"]) == {"limit"}
    assert "required" not in tool["inputSchema"]


def test_the_gateway_target_does_not_list_the_tool() -> None:
    assert TOOL not in _gateway_tool_names()
    assert TOOL not in TEMPLATE.read_text(encoding="utf-8")


def test_the_gateway_path_of_the_handler_cannot_choose_the_tool() -> None:
    handler = next(
        node
        for node in _index_tree().body
        if isinstance(node, ast.FunctionDef) and node.name == "lambda_handler"
    )
    names = {node.value for node in ast.walk(handler) if isinstance(node, ast.Constant)}
    assert TOOL not in names
    attributes = {node.attr for node in ast.walk(handler) if isinstance(node, ast.Attribute)}
    assert TOOL not in attributes


def test_the_tool_is_run_only_from_the_api_key_endpoints_dispatch() -> None:
    function = next(
        node
        for node in _index_tree().body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_tool_call"
    )
    names = {node.value for node in ast.walk(function) if isinstance(node, ast.Constant)}
    assert TOOL in names
