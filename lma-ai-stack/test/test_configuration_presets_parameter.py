# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The ConfigurationPreset stack parameter offers exactly the shipped presets."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PRESETS = ROOT / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/presets"


class _Loader(yaml.SafeLoader):
    """Reads a CloudFormation template, leaving its intrinsic functions as plain values."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _allowed(template: Path) -> set:
    document = yaml.load(template.read_text(), Loader=_Loader)  # noqa: S506 -- a SafeLoader subclass
    return set(document["Parameters"]["ConfigurationPreset"]["AllowedValues"])


def test_both_templates_offer_none_and_every_shipped_preset() -> None:
    shipped = {path.stem for path in PRESETS.glob("*.json")}
    assert shipped, "no presets are shipped"
    for template in (ROOT / "lma-main.yaml", ROOT / "lma-ai-stack/deployment/lma-ai-stack.yaml"):
        assert _allowed(template) == {"none"} | shipped, template.name


def test_every_shipped_preset_is_valid_json() -> None:
    for path in PRESETS.glob("*.json"):
        assert isinstance(json.loads(path.read_text()), dict), path.name
