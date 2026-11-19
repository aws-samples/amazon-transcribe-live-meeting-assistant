# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The two MCP endpoints describe list_meetings' ``status`` argument alike (no AWS).

The OAuth endpoint (an AgentCore Gateway target) declares its own copy of each
tool's schema in the stack template, and the API key endpoint builds its list
from ``MCP_TOOLS``. A client on either learns the accepted values only from that
description, so the two copies say the same thing, and name every value the
tool accepts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
MCP_LAMBDA = AI_STACK / "source" / "lambda_functions" / "mcp_analytics"
ACCEPTED = ("ALL", "IN_PROGRESS", "STARTED", "TRANSCRIBING", "ERRORED", "ENDED")


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


def _gateway_status_description() -> str:
    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]
    for resource in resources.values():
        config = (resource.get("Properties") or {}).get("TargetConfiguration") or {}
        payload = ((config.get("Mcp") or {}).get("Lambda") or {}).get("ToolSchema", {})
        for tool in payload.get("InlinePayload", []):
            if tool["Name"] == "list_meetings":
                return " ".join(tool["InputSchema"]["Properties"]["status"]["Description"].split())
    raise AssertionError("list_meetings is not in the gateway tool schema")


def _api_key_status_description() -> str:
    tree = ast.parse((MCP_LAMBDA / "index.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MCP_TOOLS" for t in node.targets
        ):
            tools = {tool["name"]: tool for tool in ast.literal_eval(node.value)}
            return tools["list_meetings"]["inputSchema"]["properties"]["status"]["description"]
    raise AssertionError("MCP_TOOLS not found")


def test_both_endpoints_describe_the_status_argument_in_the_same_words() -> None:
    assert _gateway_status_description() == _api_key_status_description()


def test_the_description_names_every_accepted_status() -> None:
    description = _api_key_status_description()
    for value in ACCEPTED:
        assert value in description


def test_the_accepted_values_are_the_ones_the_tool_accepts() -> None:
    tree = ast.parse((MCP_LAMBDA / "tools" / "list_meetings.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "STATUS_FILTERS":
            keys = tuple(key.value for key in node.value.keys)
            assert keys == ACCEPTED
            return
    raise AssertionError("STATUS_FILTERS not found")
