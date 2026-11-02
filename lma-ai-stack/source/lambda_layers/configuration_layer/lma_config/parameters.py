#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Runtime settings that used to be CloudFormation parameters, and the two that still are.

Until 0.4.0 many of LMA's stack parameters only ever reached application code:
a model id, a number, a switch, passed to a function as an environment variable.
Those settings live in configuration sections now, and their parameters are gone:

* ``setting_defaults.json`` beside this module holds each one's shipped default
  and type, by section and setting, with the parameter it replaced. The
  configuration seeder writes the defaults into the baseline ``Config#default``
  (:func:`shipped_setting_defaults`), and :func:`read_setting` falls back to
  them when the configuration cannot be read.
* ``parameter_defaults.json`` names the stack parameters that remain the
  defaults of settings: the two retention periods, which are an **upper limit**
  as well as the default. An administrator may keep records for less time than
  the deployment allows and not for more. Their settings carry
  ``maximumIsDefault`` in the section schema. The seeder is given their values
  as the ``ParameterDefaults`` property of its custom resource
  (:func:`apply_parameter_defaults`), the store refuses a save above the
  baseline's value, and :func:`read_setting` returns the smaller of the section's
  value and the parameter's environment variable.
* An administrator's change is stored in a Custom profile, like any other.

:func:`read_setting` is how a reader gets the value: the active configuration;
then, for a setting a parameter still backs, the parameter's environment
variable; then the caller's default; then the shipped default.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from .errors import ConfigurationError, ConfigurationTableNotConfigured, InvalidConfiguration
from .schema import limited_settings_of, validate_section
from .sections import section_spec
from .store import TABLE_NAME_ENV_VAR, ConfigurationStore

LOGGER = logging.getLogger(__name__)

PARAMETER_DEFAULTS_FILE = Path(__file__).resolve().parent / "parameter_defaults.json"
SETTING_DEFAULTS_FILE = Path(__file__).resolve().parent / "setting_defaults.json"

#: The value types a parameter-backed setting can have. CloudFormation passes
#: every custom resource property and every environment variable as text, so
#: each one is read back into its type the same way on both paths.
VALUE_TYPES = ("string", "integer", "number", "boolean")


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


@lru_cache(maxsize=1)
def _setting_defaults() -> Dict[str, Dict[str, Dict[str, Any]]]:
    raw = json.loads(SETTING_DEFAULTS_FILE.read_text(encoding="utf-8"))
    for section, settings in raw.items():
        section_spec(section)
        for setting, spec in settings.items():
            if spec["type"] not in VALUE_TYPES:
                raise ConfigurationError(
                    f"{section} › {setting} declares the type {spec['type']!r}; "
                    f"expected one of {VALUE_TYPES}"
                )
    return raw


def shipped_setting_defaults() -> Dict[str, Dict[str, Any]]:
    """The shipped default of each setting no stack parameter backs, by section."""
    return {
        section: {setting: spec["default"] for setting, spec in settings.items()}
        for section, settings in _setting_defaults().items()
    }


def _shipped(section: str, setting: str) -> Optional[Dict[str, Any]]:
    return _setting_defaults().get(section, {}).get(setting)


def _by_setting() -> Dict[Tuple[str, str], ParameterDefault]:
    entries = {
        (section, setting): ParameterDefault(
            spec.get("formerParameter", ""), section, setting, spec["type"]
        )
        for section, settings in _setting_defaults().items()
        for setting, spec in settings.items()
    }
    entries.update({(entry.section, entry.setting): entry for entry in parameter_defaults()})
    return entries


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
        # Digits only, with an optional sign: Python's int() would also take
        # "1_000", which the TypeScript half's reader does not, and the two have
        # to agree on what a value is.
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            raise ValueError(f"{raw!r} is not an integer")
        return int(text)
    if value_type == "number":
        if isinstance(raw, bool):
            raise ValueError(f"{raw!r} is not a number")
        if isinstance(raw, (int, float)):
            number = raw
        else:
            text = str(raw).strip()
            # Decimal notation only, as the TypeScript half reads it: Python's
            # float() would also take "nan", "inf" and "1_0".
            if not re.fullmatch(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)", text):
                raise ValueError(f"{raw!r} is not a number")
            number = float(text)
        if not math.isfinite(number):
            raise ValueError(f"{raw!r} is not a number")
        return int(number) if float(number).is_integer() else float(number)
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
            # An empty value is what a nested stack deployed on its own passes
            # for a parameter only lma-main.yaml supplies, so it is expected
            # there and logged quietly; anything else is worth a warning. (An
            # empty value for a boolean or integer setting is refused by the
            # type check above instead, and logged there.)
            log = LOGGER.info if values[entry.parameter] in ("", None) else LOGGER.warning
            log(
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
    env_var: str = "",
    default: Any = None,
    store: Optional[ConfigurationStore] = None,
) -> Any:
    """The value of a runtime setting.

    In order of preference: the section's resolved value (an administrator's
    change, or else the baseline the seeder wrote); the environment variable
    ``env_var`` names, for a setting a stack parameter still backs; ``default``;
    and the setting's shipped default (``setting_defaults.json``), so a reader
    that cannot reach the configuration still has a value.

    Call it where the value is used, not at import, so the section's cache tier
    decides how soon a change is seen. ``store`` lets a function that already
    holds a store share its cache; otherwise one store per process is built from
    ``CONFIGURATION_TABLE_NAME``.

    For a setting whose parameter is also its upper limit (``maximumIsDefault``
    in the section schema), the answer is the smaller of the section's value and
    the environment variable's, or ``default`` standing in for the variable. A
    section value that is not above zero is not used.

    Raises:
        KeyError: neither file lists ``section`` › ``setting``. The layer's tests
            and the readers' own tests hold every call to one that is listed.
    """
    entry = _by_setting()[(section, setting)]
    if setting in limited_settings_of(section):
        return _limited(
            entry,
            _configured(entry, store, env_var or "the shipped default"),
            _from_environment(entry, env_var, default),
        )
    configured = _configured(entry, store, env_var or "the shipped default")
    if configured is not None:
        return configured
    value = _from_environment(entry, env_var, default) if env_var else default
    if value is None:
        shipped = _shipped(section, setting)
        if shipped is not None:
            return shipped["default"]
    return value


def _limited(entry: ParameterDefault, configured: Any, limit: Any) -> Any:
    """The configured value, never above the parameter's (``maximumIsDefault``)."""
    # The schema refuses a save of zero or less, which would expire a record as
    # it is written; a value that reached the profile another way is not used.
    if configured is None or configured <= 0:
        return limit
    if limit is None:
        return configured
    if configured > limit:
        LOGGER.warning(
            "%s › %s is %s, above this deployment's limit of %s; using %s",
            entry.section,
            entry.setting,
            configured,
            limit,
            limit,
        )
        return limit
    return configured


def _configured(entry: ParameterDefault, store: Optional[ConfigurationStore], env_var: str) -> Any:
    """The section's resolved value of the setting, or None when it holds none."""
    section, setting = entry.section, entry.setting
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
    return None


def _from_environment(entry: ParameterDefault, env_var: str, default: Any) -> Any:
    """The parameter's environment variable, read as the setting's type; else ``default``."""
    raw = os.environ.get(env_var)
    if raw is None or (raw == "" and entry.value_type != "string"):
        return default
    try:
        return coerce(entry.value_type, raw)
    except ValueError:
        LOGGER.warning("%s=%r is not a %s; using %r", env_var, raw, entry.value_type, default)
        return default
