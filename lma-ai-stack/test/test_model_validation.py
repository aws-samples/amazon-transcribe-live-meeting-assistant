# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Deploy-time model validation checks the models a new deployment calls."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SETTING_DEFAULTS = (
    ROOT / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/setting_defaults.json"
)


class _Loader(yaml.SafeLoader):
    """Reads a CloudFormation template, leaving its intrinsic functions as plain values."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _validated_models() -> list:
    document = yaml.load((ROOT / "lma-main.yaml").read_text(), Loader=_Loader)  # noqa: S506 -- a SafeLoader subclass
    return document["Resources"]["ValidateParameters"]["Properties"]["BedrockModelIds"]


def test_exactly_the_shipped_models_every_deployment_calls_are_validated() -> None:
    """A default that changes takes the old id out of the list too."""
    models = json.loads(SETTING_DEFAULTS.read_text())["language_models"]
    expected = {
        models[setting]["default"]
        for setting in ("summaryModelId", "meetingAssistModelId", "knowledgeBaseModelId")
    }
    expected.add("amazon.titan-embed-text-v2:0")
    assert set(_validated_models()) == expected
