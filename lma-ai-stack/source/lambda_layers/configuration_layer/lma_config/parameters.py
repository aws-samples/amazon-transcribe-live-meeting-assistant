#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Runtime settings whose deployment default is a CloudFormation parameter.

Some of LMA's stack parameters only ever reached application code: a model id,
a number, a switch, passed to a function as an environment variable and read
there. Those settings now live in a configuration section, so an administrator
can change them on the Configuration page, and the parameter stays as the
deployment's **default** for the setting:

* ``parameter_defaults.json`` beside this module names, for each such parameter,
  the section and setting it is the default of, and the setting's type.
* The configuration seeder is given the parameters' values as the
  ``ParameterDefaults`` property of its custom resource, and
  :func:`apply_parameter_defaults` writes them into the baseline
  ``Config#default``. A stack update that changes a parameter changes that
  property, so the seeder runs again and the baseline follows.
* An administrator's change is stored in the profile, like any other, and wins
  over the baseline until it is reset.
* :func:`read_setting` is how a reader gets the value. The function or task keeps
  the parameter's environment variable, which it falls back to whenever the
  section does not hold the setting or the table cannot be read -- so a reader
  deployed ahead of the seeder, or a deployment whose table is unreachable, keeps
  behaving as the parameter says rather than failing.

The parameters are kept, not removed: existing deploy commands and pipelines pass
them, and removing one would make those fail.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from .errors import ConfigurationError, ConfigurationTableNotConfigured, InvalidConfiguration
from .schema import validate_section
from .sections import section_spec
from .store import TABLE_NAME_ENV_VAR, ConfigurationStore

LOGGER = logging.getLogger(__name__)

PARAMETER_DEFAULTS_FILE = Path(__file__).resolve().parent / "parameter_defaults.json"

#: The value types a parameter-backed setting can have. CloudFormation passes
#: every custom resource property and every environment variable as text, so
#: each one is read back into its type the same way on both paths.
VALUE_TYPES = ("string", "integer", "boolean")


class ParameterDefault(NamedTuple):
    """One stack parameter that is the default of one runtime setting."""

    parameter: str
    section: str
    setting: str
    value_type: str


@lru_cache(maxsize=1)
def parameter_defaults() -> Tuple[ParameterDefault, ...]:
    """Every parameter-backed setting, in the order the file lists them.

    Raises:
        ConfigurationError: the file names a section that is not registered, or a
            type this module cannot read. The layer's tests load it, so this is a
            release-time failure rather than a deploy-time one.
    """
    raw = json.loads(PARAMETER_DEFAULTS_FILE.read_text(encoding="utf-8"))
    entries = []
    for parameter, spec in raw.items():
        section_spec(spec["section"])
        if spec["type"] not in VALUE_TYPES:
            raise ConfigurationError(
                f"{parameter} declares the type {spec['type']!r}; expected one of {VALUE_TYPES}"
            )
        entries.append(ParameterDefault(parameter, spec["section"], spec["setting"], spec["type"]))
    return tuple(entries)


def _by_setting() -> Dict[Tuple[str, str], ParameterDefault]:
    return {(entry.section, entry.setting): entry for entry in parameter_defaults()}


def coerce(value_type: str, raw: Any) -> Any:
    """``raw`` read as a value of ``value_type``.

    Raises:
        ValueError: ``raw`` is not a value of that type.
    """
    if value_type == "string":
        return "" if raw is None else str(raw)
    if value_type == "integer":
        if isinstance(raw, bool):
            raise ValueError(f"{raw!r} is not an integer")
        if isinstance(raw, int):
            return raw
        text = str(raw).strip()
        return int(text)
    if value_type == "boolean":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "false"):
            return text == "true"
        raise ValueError(f"{raw!r} is not true or false")
    raise ValueError(f"unknown value type {value_type!r}")


def apply_parameter_defaults(baseline: Dict[str, Any], values: Mapping[str, Any]) -> List[str]:
    """Write each parameter's value into its setting of ``baseline``, in place.

    ``values`` is the ``ParameterDefaults`` property of the seeder's custom
    resource: parameter name to value. A parameter the property does not name is
    left out, so its setting stays absent from the baseline and every reader
    falls back to its environment variable -- which is the state of a template
    that predates the setting. A value that cannot be read as its type, or that
    the section's schema refuses, is left out the same way, and logged: the
    baseline is never checked on its way in, so this is the one place a value an
    administrator's save would refuse could otherwise enter it. That covers a
    nested stack deployed on its own, whose model parameters default to empty.
    Names the property holds that no setting is backed by are logged and
    ignored.

    Returns:
        The parameters that were applied, in file order.
    """
    known = {entry.parameter: entry for entry in parameter_defaults()}
    for name in sorted(set(values) - set(known)):
        LOGGER.warning("ignoring the parameter default %s: no setting is backed by it", name)
    applied = []
    for entry in parameter_defaults():
        if entry.parameter not in values:
            continue
        try:
            value = coerce(entry.value_type, values[entry.parameter])
        except ValueError:
            LOGGER.warning(
                "the %s parameter's value %r is not a %s; leaving %s › %s to the "
                "readers' environment variable",
                entry.parameter,
                values[entry.parameter],
                entry.value_type,
                entry.section,
                entry.setting,
            )
            continue
        try:
            validate_section(entry.section, {entry.setting: value})
        except InvalidConfiguration as error:
            LOGGER.warning(
                "the %s parameter's value is not one the %s section accepts (%s); leaving "
                "%s to the readers' environment variable",
                entry.parameter,
                entry.section,
                error,
                entry.setting,
            )
            continue
        section = baseline.setdefault(entry.section, {})
        section[entry.setting] = value
        applied.append(entry.parameter)
    return applied


_SHARED_STORE: Optional[ConfigurationStore] = None


def _shared_store() -> Optional[ConfigurationStore]:
    """One store per process, so the section's cache tier spans invocations."""
    global _SHARED_STORE  # noqa: PLW0603 -- the per-process cache is the point
    if _SHARED_STORE is None and os.environ.get(TABLE_NAME_ENV_VAR):
        _SHARED_STORE = ConfigurationStore()
    return _SHARED_STORE


def read_setting(
    section: str,
    setting: str,
    *,
    env_var: str,
    default: Any = None,
    store: Optional[ConfigurationStore] = None,
) -> Any:
    """The value of a parameter-backed setting.

    In order of preference: the section's resolved value (an administrator's
    change, or else the baseline the seeder wrote from the parameter); the
    parameter's environment variable on this function or task; ``default``.

    Call it where the value is used, not at import, so the section's cache tier
    decides how soon a change is seen. ``store`` lets a function that already
    holds a store share its cache; otherwise one store per process is built from
    ``CONFIGURATION_TABLE_NAME``, and with that unset the environment variable is
    the answer.

    Raises:
        KeyError: no parameter is the default of ``section`` › ``setting``. Every
            call names one that ``parameter_defaults.json`` lists; the layer's
            tests and the readers' own tests hold to that.
    """
    entry = _by_setting()[(section, setting)]
    reader = store if store is not None else _shared_store()
    if reader is not None:
        try:
            values = reader.get_section(section).values
        except (ClientError, BotoCoreError, ConfigurationTableNotConfigured, ConfigurationError):
            LOGGER.warning(
                "could not read the %s configuration section; using %s for %s",
                section,
                env_var,
                setting,
                exc_info=True,
            )
        else:
            if setting in values and values[setting] is not None:
                try:
                    return coerce(entry.value_type, values[setting])
                except ValueError:
                    LOGGER.warning(
                        "the stored %s › %s, %r, is not a %s; using %s",
                        section,
                        setting,
                        values[setting],
                        entry.value_type,
                        env_var,
                    )
    raw = os.environ.get(env_var)
    if raw is None or (raw == "" and entry.value_type != "string"):
        return default
    try:
        return coerce(entry.value_type, raw)
    except ValueError:
        LOGGER.warning("%s=%r is not a %s; using %r", env_var, raw, entry.value_type, default)
        return default
