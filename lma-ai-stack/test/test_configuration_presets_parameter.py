# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The ConfigurationPreset stack parameter offers the shipped presets.

They come in their order, with Standard first and the default, followed by
``none``, the parameter's value in earlier pre-releases, which a stack deployed
then keeps and the seeder reads as Standard.
"""

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PRESETS = ROOT / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/presets"
TEMPLATES = (ROOT / "lma-main.yaml", ROOT / "lma-ai-stack/deployment/lma-ai-stack.yaml")


class _Loader(yaml.SafeLoader):
    """Reads a CloudFormation template, leaving its intrinsic functions as plain values."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _parameter(template: Path) -> dict:
    document = yaml.load(template.read_text(), Loader=_Loader)  # noqa: S506 -- a SafeLoader subclass
    return document["Parameters"]["ConfigurationPreset"]


def _shipped_in_order() -> list:
    documents = {path.stem: json.loads(path.read_text()) for path in PRESETS.glob("*.json")}
    return [name for _order, name in sorted((d.get("order", 0), n) for n, d in documents.items())]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_the_parameter_offers_the_shipped_presets_then_none(template) -> None:
    shipped = _shipped_in_order()
    assert shipped[0] == "standard"
    parameter = _parameter(template)
    assert parameter["Default"] == "standard"
    assert parameter["AllowedValues"] == [*shipped, "none"]


def test_every_shipped_preset_is_valid_json() -> None:
    for path in PRESETS.glob("*.json"):
        assert isinstance(json.loads(path.read_text()), dict), path.name
