#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The schema of each configuration section, and validation against it.

Every registered section has one JSON Schema document, ``schemas/<section>.json``
beside this module. The same document does two jobs:

* **It is what a save is checked against.** :func:`validate_section` is called by
  the store on every operator save, whichever resolver it arrives through, so a
  value the readers could not use is refused when it is saved rather than
  discovered when a meeting reads it.
* **It is what the configuration editor renders.** The documents carry UI-layout
  annotations next to the validation keywords (``order``, ``sectionLabel``,
  ``format: textarea`` and so on, listed in :data:`UI_ANNOTATIONS`), and the
  resolver behind ``Query.getConfiguration`` serves them to the web UI as they
  stand.

The files ship inside this layer, so the schema a save is checked against is
always the one the code reading the section was released with.

What a schema describes is the section **as stored in a profile**: a delta over
the shipped baseline. That is why no section schema lists ``required``
properties at its top level -- a profile that changes one Nova Sonic setting
stores that one setting -- and why the shipped baseline is checked against the
same documents by the layer's tests.

The validator implements the subset of JSON Schema (2020-12) these documents use,
with the standard library only: the layer has no third-party dependencies, so it
can be attached to functions on either architecture. A keyword it does not
implement is refused by the tests rather than silently ignored, because an
ignored keyword is a constraint that looks enforced and is not. The web UI holds
a JavaScript copy of the same validator, for an editor to check a value before
it sends the save (``lma-ai-stack/source/ui/src/components/configuration/
schema-validation.js``); ``lma-ai-stack/test/fixtures/
configuration_schema_cases.json`` is run through both, so the two cannot drift
apart unnoticed.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import InvalidConfiguration, UnknownConfigurationSection
from .sections import registered_sections, section_spec

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

#: Validation keywords the validator implements. A schema using any other
#: keyword that is not a UI annotation fails the layer's tests.
VALIDATION_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "type",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "properties",
        "patternProperties",
        "additionalProperties",
        "propertyNames",
        "required",
        "items",
        "minItems",
        "maxItems",
        "maxProperties",
    }
)

#: Keywords that describe presentation only. The validator ignores them; the
#: configuration editor reads them. Documented in docs/configuration-store.md.
UI_ANNOTATIONS = frozenset(
    {
        "title",
        "description",
        "examples",
        # Ordering and grouping, as IDP's configuration schema uses them.
        "order",
        "sectionLabel",
        "collapsible",
        "defaultExpanded",
        "listLabel",
        "itemLabel",
        "keyLabel",
        "enumLabels",
        "booleanLabels",
        "format",
        "rows",
        "widget",
        "hidden",
        # Show a field only while another field of the same object has a value.
        "dependsOn",
        # Show a field or section only when a deployment setting has a value.
        "availableWhen",
        # How a profile's section combines with the baseline's: "merge" key by
        # key (the store's deep merge), or "replace", where a non-empty profile
        # section is the whole value. Matches what the section's readers do.
        "mergeStrategy",
        # Keys of the form "N#Label", where N orders the entries.
        "numberedKeys",
        # The value a profile stores to turn a shipped entry off.
        "disabledValue",
    }
)

#: How a message names each type. The UI's copy uses the same words.
_TYPE_WORDS = {
    "object": "an object",
    "array": "a list",
    "string": "text",
    "number": "a number",
    "integer": "a whole number",
    "boolean": "true or false",
    "null": "null",
}
_TYPE_NAMES = tuple(_TYPE_WORDS)


@lru_cache(maxsize=None)
def _load(name: str) -> str:
    return (SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8")


def section_schema(name: str) -> Dict[str, Any]:
    """The schema document for a registered section, as a fresh copy.

    Raises:
        UnknownConfigurationSection: ``name`` is not registered, or has no schema.
    """
    section_spec(name)
    try:
        return json.loads(_load(name))
    except FileNotFoundError:
        raise UnknownConfigurationSection(
            f"the configuration section {name!r} is registered but ships no schema "
            f"({SCHEMA_DIR.name}/{name}.json)"
        ) from None


def section_schemas() -> Dict[str, Dict[str, Any]]:
    """Every registered section's schema, keyed by section name."""
    return {name: section_schema(name) for name in sorted(registered_sections())}


def format_path(path: Sequence[str]) -> str:
    """A path for a message: keys joined by " › ".

    Not a JSON pointer: keys here routinely hold "/", "#" and "." of their own
    ("bedrock/amazon.nova-lite-v1:0", "1#SUMMARY"), and a separator that cannot
    appear in a key keeps the message readable.
    """
    return " › ".join(path) if path else "(the value)"


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # Python's json module reads NaN and Infinity; JSON Schema has no such
    # numbers, and a price of Infinity would make every estimate wrong at once.
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if expected == "integer":
        return isinstance(value, int) or float(value).is_integer()
    return expected == "number"


def _json(value: Any) -> str:
    """A value as a message quotes it: JSON, with non-ASCII text left as it is.

    ``ensure_ascii=False`` because the UI's copy uses ``JSON.stringify``, which
    does not escape it either.
    """
    return json.dumps(value, ensure_ascii=False)


def _count(number: Any, singular: str, plural: str = "") -> str:
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _same(value: Any, option: Any) -> bool:
    """Equality as JSON sees it: ``true`` is not ``1``, which Python's ``==`` says it is."""
    if isinstance(value, bool) or isinstance(option, bool):
        return isinstance(value, bool) and isinstance(option, bool) and value == option
    return bool(value == option)


def _describe(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if value is None:
        return "null"
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, str):
        return "text"
    if isinstance(value, float) and not math.isfinite(value):
        return "a non-finite number"
    return "a number"


def check(schema: Mapping[str, Any], value: Any, path: Tuple[str, ...] = ()) -> List[str]:
    """Every problem with ``value`` against ``schema``, as messages naming the path.

    Empty when the value is valid. The JavaScript copy in the web UI
    (``ui/src/components/configuration/schema-validation.js``) produces the same
    problems in the same order; the shared fixture asserts it.
    """
    problems: List[str] = []
    where = format_path(path)

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_type_matches(t, value) for t in expected_types):
            names = " or ".join(_TYPE_WORDS.get(t, t) for t in expected_types)
            problems.append(f"{where}: must be {names}; got {_describe(value)}")
            # Nothing below means anything for a value of the wrong type.
            return problems

    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        allowed = ", ".join(_json(option) for option in schema["enum"])
        problems.append(f"{where}: must be one of {allowed}; got {_json(value)}")

    if _type_matches("number", value):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where}: must be at least {schema['minimum']}; got {value}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{where}: must be at most {schema['maximum']}; got {value}")

    if isinstance(value, str):
        # Code points, as JSON Schema counts them; Python's len and the UI's
        # Array.from agree on that, where String.length would count UTF-16 units.
        length = len(value)
        if "minLength" in schema and length < schema["minLength"]:
            problems.append(
                f"{where}: must be at least {schema['minLength']} characters"
                if schema["minLength"] > 1
                else f"{where}: must not be empty"
            )
        if "maxLength" in schema and length > schema["maxLength"]:
            problems.append(
                f"{where}: must be at most {schema['maxLength']} characters; got {length}"
            )
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{where}: {_json(value)} does not match {schema['pattern']}")

    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{where}: must hold at least {_count(schema['minItems'], 'item')}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where}: must hold at most {_count(schema['maxItems'], 'item')}")
        if isinstance(schema.get("items"), Mapping):
            for position, item in enumerate(value):
                problems.extend(check(schema["items"], item, path + (f"item {position + 1}",)))

    if isinstance(value, Mapping):
        problems.extend(_check_object(schema, value, path))

    return problems


def _check_object(
    schema: Mapping[str, Any], value: Mapping[str, Any], path: Tuple[str, ...]
) -> List[str]:
    problems: List[str] = []
    where = format_path(path)
    for name in schema.get("required", []):
        if name not in value:
            problems.append(f"{where}: {_json(name)} is required")
    if "maxProperties" in schema and len(value) > schema["maxProperties"]:
        problems.append(
            f"{where}: must hold at most {_count(schema['maxProperties'], 'entry', 'entries')}"
        )

    properties = schema.get("properties") or {}
    patterns = schema.get("patternProperties") or {}
    additional = schema.get("additionalProperties", True)
    names_schema = schema.get("propertyNames")
    for key, item in value.items():
        key = str(key)
        child = path + (key,)
        if isinstance(names_schema, Mapping):
            pattern = names_schema.get("pattern")
            if pattern is not None and not re.search(pattern, key):
                problems.append(f"{format_path(child)}: the name does not match {pattern}")
                continue
        matched = False
        if key in properties:
            matched = True
            problems.extend(check(properties[key], item, child))
        for pattern, pattern_schema in patterns.items():
            if re.search(pattern, key):
                matched = True
                problems.extend(check(pattern_schema, item, child))
        if matched:
            continue
        if additional is False:
            problems.append(f"{format_path(child)}: is not a setting this section has")
        elif isinstance(additional, Mapping):
            problems.extend(check(additional, item, child))
    return problems


def validate_section(name: str, values: Any) -> None:
    """Refuse ``values`` as the stored contents of section ``name`` if they are invalid.

    Raises:
        UnknownConfigurationSection: ``name`` is not registered.
        InvalidConfiguration: the values do not match the section's schema. The
            exception's ``problems`` lists them, each path starting with the
            section's name.
    """
    schema = section_schema(name)
    problems = check(schema, values, (name,))
    if problems:
        raise InvalidConfiguration(problems)


def unknown_keywords(schema: Any, path: Tuple[str, ...] = ()) -> List[str]:
    """Keywords in ``schema`` that are neither implemented nor UI annotations.

    For the layer's tests. Walks every subschema position the validator reads.
    """
    if not isinstance(schema, Mapping):
        return []
    found = [
        f"{format_path(path)}: {key}"
        for key in schema
        if key not in VALIDATION_KEYWORDS and key not in UI_ANNOTATIONS
    ]
    expected = schema.get("type")
    for type_name in [expected] if isinstance(expected, str) else list(expected or []):
        if type_name not in _TYPE_NAMES:
            found.append(f"{format_path(path)}: type {type_name!r}")
    for key, child in (schema.get("properties") or {}).items():
        found.extend(unknown_keywords(child, path + (key,)))
    for pattern, child in (schema.get("patternProperties") or {}).items():
        found.extend(unknown_keywords(child, path + (pattern,)))
    for key in ("additionalProperties", "items", "propertyNames"):
        found.extend(unknown_keywords(schema.get(key), path + (key,)))
    return found
