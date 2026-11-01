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


class InvalidPreset(ConfigurationError):
    """A shipped preset is malformed. A release with one should fail its tests."""


def _checked(preset_id: str, document: Any) -> Dict[str, Any]:
    if not PRESET_ID.match(preset_id):
        raise InvalidPreset(f"preset id {preset_id!r} is not lower-case words joined by hyphens")
    if not isinstance(document, dict) or set(document) != {"title", "description", "sections"}:
        raise InvalidPreset(f"preset {preset_id}: needs exactly title, description and sections")
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
    return {
        "Id": preset_id,
        "Title": document["title"],
        "Description": document["description"],
        "Sections": sections,
    }


@lru_cache(maxsize=1)
def _load() -> str:
    presets = [
        _checked(path.stem, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(PRESET_DIR.glob("*.json"))
    ]
    return json.dumps(presets, sort_keys=True)


def shipped_presets() -> List[Dict[str, Any]]:
    """Every shipped preset, as a fresh copy, sorted by id.

    Raises:
        InvalidPreset: a shipped preset is malformed.
    """
    return json.loads(_load())
