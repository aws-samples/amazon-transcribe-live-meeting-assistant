# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The ConfigurationPreset stack parameter offers the shipped organization presets.

They come first, in their order, with Standard the default. After them come the
values the parameter offered before the organization presets were added, which a
stack deployed then keeps as its value: ``none`` and the add-on ids. The seeder
accepts each of those (see its tests).
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


def _shipped() -> dict:
    return {path.stem: json.loads(path.read_text()) for path in PRESETS.glob("*.json")}


def _of_kind(kind: str) -> list:
    presets = [
        (document.get("order", 0), name)
        for name, document in _shipped().items()
        if document["kind"] == kind
    ]
    return [name for _order, name in sorted(presets)]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_the_organization_presets_come_first_and_standard_is_the_default(template) -> None:
    organization = _of_kind("organization")
    assert organization[0] == "standard"
    parameter = _parameter(template)
    assert parameter["Default"] == "standard"
    assert parameter["AllowedValues"][: len(organization)] == organization


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_the_other_allowed_values_are_none_and_the_add_ons(template) -> None:
    organization = _of_kind("organization")
    rest = _parameter(template)["AllowedValues"][len(organization) :]
    assert rest == ["none", *_of_kind("add-on")]


def test_every_shipped_preset_is_valid_json_of_a_known_kind() -> None:
    for name, document in _shipped().items():
        assert isinstance(document, dict), name
        assert document["kind"] in ("organization", "add-on"), name
