#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The configuration library: named presets an administrator can apply.

Each preset is ``presets/<id>.json`` beside this module, shipped in the layer
like the section schemas, and served to the configuration editor by
``Query.getConfiguration``. A preset never saves anything by itself: the editor
applies it to the page as unsaved changes, which the administrator reviews and
saves like any other edit, so it is checked against the schemas and recorded as
a revision.

A preset is one of two kinds, named by its ``kind``:

``organization``
    A complete organizational default for consent, retention and privacy. Every
    organization preset sets the same settings, each section by ``merge``, so
    applying one sets all of them, whichever was applied before, and leaves every
    other setting as it is. They are the choices of the ConfigurationPreset stack
    parameter, and the seeder writes each as a Managed profile.
``add-on``
    A change to one feature, applied over whatever profile is open. Offered in
    the editor only.

An optional integer ``order`` places a preset among those of its kind.

A preset names the sections it changes, and for each one how:

``merge``
    Its values are laid over the section as it stands, key by key: the settings
    it names change and every other one stays.
``replace``
    The section becomes exactly its values. In a section whose entries the
    profile can turn off (the summary prompts), every shipped entry the preset
    does not name is turned off.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from .errors import ConfigurationError
from .schema import validate_section
from .sections import registered_sections

PRESET_DIR = Path(__file__).resolve().parent / "presets"

#: A preset's id is its file name; restricted so it reads safely in a URL or a log.
PRESET_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

PRESET_MODES = ("merge", "replace")

ORGANIZATION = "organization"
ADD_ON = "add-on"
PRESET_KINDS = (ORGANIZATION, ADD_ON)

_REQUIRED_KEYS = {"title", "description", "kind", "sections"}
_OPTIONAL_KEYS = {"order"}


class InvalidPreset(ConfigurationError):
    """A shipped preset is malformed. A release with one should fail its tests."""


def _checked(preset_id: str, document: Any) -> Dict[str, Any]:
    if not PRESET_ID.match(preset_id):
        raise InvalidPreset(f"preset id {preset_id!r} is not lower-case words joined by hyphens")
    if (
        not isinstance(document, dict)
        or not _REQUIRED_KEYS <= set(document)
        or set(document) - _REQUIRED_KEYS - _OPTIONAL_KEYS
    ):
        raise InvalidPreset(
            f"preset {preset_id}: needs exactly title, description, kind and sections, "
            "and may have an order"
        )
    if document["kind"] not in PRESET_KINDS:
        raise InvalidPreset(f"preset {preset_id}: kind must be one of {PRESET_KINDS}")
    order = document.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise InvalidPreset(f"preset {preset_id}: order must be an integer")
    sections = document["sections"]
    if not isinstance(sections, dict) or not sections:
        raise InvalidPreset(f"preset {preset_id}: sections must name at least one section")
    known = registered_sections()
    for name, change in sections.items():
        if name not in known:
            raise InvalidPreset(f"preset {preset_id}: {name!r} is not a configuration section")
        if not isinstance(change, dict) or set(change) != {"mode", "values"}:
            raise InvalidPreset(f"preset {preset_id}: {name} needs exactly mode and values")
        if change["mode"] not in PRESET_MODES:
            raise InvalidPreset(f"preset {preset_id}: {name} mode must be one of {PRESET_MODES}")
        # A preset's values are stored, after the editor applies them, as part
        # of a section's delta, so each must be valid on its own.
        validate_section(name, change["values"])
        if document["kind"] == ORGANIZATION and change["mode"] != "merge":
            raise InvalidPreset(f"preset {preset_id}: an organization preset only merges")
    return {
        "Id": preset_id,
        "Title": document["title"],
        "Description": document["description"],
        "Kind": document["kind"],
        "Order": order,
        "Sections": sections,
    }


def _settings_named(preset: Dict[str, Any]) -> List[str]:
    return sorted(
        f"{name}.{key}" for name, change in preset["Sections"].items() for key in change["values"]
    )


def _check_organization_presets(presets: List[Dict[str, Any]]) -> None:
    organization = [preset for preset in presets if preset["Kind"] == ORGANIZATION]
    if not organization:
        return
    expected = _settings_named(organization[0])
    for preset in organization[1:]:
        if _settings_named(preset) != expected:
            raise InvalidPreset(
                f"preset {preset['Id']}: an organization preset must set the same settings "
                f"as {organization[0]['Id']}"
            )


@lru_cache(maxsize=1)
def _load() -> str:
    presets = [
        _checked(path.stem, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(PRESET_DIR.glob("*.json"))
    ]
    _check_organization_presets(presets)
    return json.dumps(presets, sort_keys=True)


def shipped_presets() -> List[Dict[str, Any]]:
    """Every shipped preset, as a fresh copy, sorted by id.

    Raises:
        InvalidPreset: a shipped preset is malformed.
    """
    return json.loads(_load())


def organization_presets() -> List[Dict[str, Any]]:
    """The shipped organization presets, in their ``order``."""
    return sorted(
        (preset for preset in shipped_presets() if preset["Kind"] == ORGANIZATION),
        key=lambda preset: (preset["Order"], preset["Id"]),
    )


# ── applying a preset ──────────────────────────────────────────────────────
#
# The same rules the configuration editor applies (config-model.js:
# isPresetOffered, replacedSection and applyPresetSection), for the deploy-time
# seeder, which writes each organization preset as a Managed profile and may build the
# baseline itself from one (the ConfigurationPreset stack parameter).


def _available(schema: Any, settings: Dict[str, Any]) -> bool:
    condition = schema.get("availableWhen") if isinstance(schema, dict) else None
    if not condition:
        return True
    return str(settings.get(condition["setting"])) == str(condition["value"])


def preset_offered(preset: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    """Whether a deployment offers every section and top-level setting a preset sets.

    ``settings`` are the deployment settings an ``availableWhen`` names, such as
    ``AsrEngineAvailable``. A preset is applied whole or not at all.
    """
    from .schema import section_schema  # pylint: disable=import-outside-toplevel

    for name, change in preset["Sections"].items():
        schema = section_schema(name)
        if not _available(schema, settings):
            return False
        properties = schema.get("properties") or {}
        if not all(_available(properties.get(key), settings) for key in change["values"]):
            return False
    return True


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_preset(body: Dict[str, Any], preset: Dict[str, Any]) -> Dict[str, Any]:
    """``body`` with a preset applied to each section it names.

    "merge" lays the preset's values over the section. "replace", or a section
    whose schema's mergeStrategy is "replace", makes the section exactly the
    values: for a merge-strategy section, the shipped section with the values
    over it and every shipped entry they do not name turned off with the
    schema's ``disabledValue``, where it has one.
    """
    from .schema import section_schema  # pylint: disable=import-outside-toplevel

    result = json.loads(json.dumps(body))
    for name, change in preset["Sections"].items():
        schema = section_schema(name)
        current = result.get(name) or {}
        values = json.loads(json.dumps(change["values"]))
        if schema.get("mergeStrategy") == "replace":
            result[name] = values
        elif change["mode"] == "replace":
            section = _deep_merge(current, values)
            disabled = schema.get("disabledValue")
            if disabled is not None:
                for key in current:
                    if key not in values:
                        section[key] = disabled
            result[name] = section
        else:
            result[name] = _deep_merge(current, values)
    return result


def preset_by_id(preset_id: str) -> Dict[str, Any]:
    """One shipped preset.

    Raises:
        InvalidPreset: no preset ships with that id.
    """
    for preset in shipped_presets():
        if preset["Id"] == preset_id:
            return preset
    raise InvalidPreset(f"no configuration preset ships with the id {preset_id!r}")
