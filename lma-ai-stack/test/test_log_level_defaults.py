# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The `LogLevel` parameter defaults to INFO in every template that declares it.

At INFO and above the functions and containers log identifiers, sizes and
outcomes; meeting content is written only at DEBUG. A deployment that takes the
defaults therefore logs at INFO, and a nested stack deployed on its own (or one
that `lma-main.yaml` does not pass `LogLevel` to) does the same.

Scoped to the templates in the root `Makefile`'s `CFN_TEMPLATES` list, so a new
template that declares `LogLevel` is covered without being added here.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = "lma-main.yaml"
MAKEFILE = REPO_ROOT / "Makefile"

MAKE_VARIABLES = {
    "$(AI_STACK_DIR)": "lma-ai-stack",
    "$(ASR_DIR)": "lma-asr-microvm-stack",
    "$(VP_DIR)": "lma-virtual-participant-stack",
    "$(WEBSOCKET_DIR)": "lma-websocket-transcriber-stack",
}


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


def _cfn_templates() -> list[str]:
    """The `CFN_TEMPLATES` list from the root Makefile, as repo-relative paths."""
    body = MAKEFILE.read_text().split("CFN_TEMPLATES := \\", 1)[1]
    paths = []
    for line in body.splitlines():
        entry = line.strip().rstrip("\\").strip()
        if not entry:
            if paths:
                break
            continue
        for variable, directory in MAKE_VARIABLES.items():
            entry = entry.replace(variable, directory)
        paths.append(entry)
    return paths


def _log_level_parameter(path: str) -> dict | None:
    template = yaml.load((REPO_ROOT / path).read_text(), Loader=_CfnLoader)
    return (template.get("Parameters") or {}).get("LogLevel")


TEMPLATES_WITH_LOG_LEVEL = [path for path in _cfn_templates() if _log_level_parameter(path)]


def test_main_template_log_level_defaults_to_info():
    parameter = _log_level_parameter(MAIN_TEMPLATE)
    assert parameter is not None, "lma-main.yaml no longer declares LogLevel"
    assert parameter["Default"] == "INFO"
    assert "DEBUG" in parameter["AllowedValues"], "DEBUG stays available for diagnosis"


def test_the_nested_templates_that_declare_log_level_are_found():
    # The main template and at least the AI, transcriber and VP stacks.
    assert MAIN_TEMPLATE in TEMPLATES_WITH_LOG_LEVEL
    assert len(TEMPLATES_WITH_LOG_LEVEL) >= 4, TEMPLATES_WITH_LOG_LEVEL


@pytest.mark.parametrize("template", TEMPLATES_WITH_LOG_LEVEL)
def test_every_template_log_level_defaults_to_info(template: str):
    assert _log_level_parameter(template)["Default"] == "INFO"
