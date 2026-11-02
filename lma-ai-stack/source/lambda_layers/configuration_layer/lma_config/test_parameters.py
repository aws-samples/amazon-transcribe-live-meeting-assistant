#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the settings whose deployment default is a stack parameter.

What is asserted:

* **Every entry names a setting its section's schema declares, with the same
  type.** A default written into a setting the schema does not know would be one
  the Configuration page cannot show and a save would refuse.
* **The seeder's write lays each parameter over the baseline, and leaves out
  what it cannot read**, so a reader falls back to its environment variable
  rather than reading a value of the wrong type.
* **A reader prefers the section, then the environment variable, then its own
  default**, and a table it cannot read is the environment variable's answer,
  not a failure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from lma_config import parameters, schema, sections
from lma_config.errors import ConfigurationTableNotConfigured


class _Store:
    """Answers get_section from a dict, or raises what it is given."""

    def __init__(self, sections_=None, error=None):
        self.sections = sections_ or {}
        self.error = error
        self.reads = 0

    def get_section(self, name):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(values=self.sections.get(name, {}))


@pytest.fixture(autouse=True)
def _no_shared_store(monkeypatch):
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    monkeypatch.setattr(parameters, "_SHARED_STORE", None)


JSON_TYPE = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}


def test_the_file_lists_at_least_one_parameter():
    assert parameters.parameter_defaults()


@pytest.mark.parametrize("entry", parameters.parameter_defaults(), ids=lambda e: e.parameter)
def test_every_entry_names_a_setting_its_section_schema_declares(entry):
    assert entry.section in sections.registered_sections()
    declared = schema.section_schema(entry.section)["properties"]
    assert entry.setting in declared, (
        f"{entry.parameter} is the default of {entry.section} › {entry.setting}, "
        "which the section's schema does not declare"
    )
    assert declared[entry.setting]["type"] == JSON_TYPE[entry.value_type]


def test_no_setting_is_backed_by_two_parameters():
    settings = [(e.section, e.setting) for e in parameters.parameter_defaults()]
    assert len(settings) == len(set(settings))


@pytest.mark.parametrize(
    "value_type, raw, expected",
    [
        ("string", "us.amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"),
        ("string", "", ""),
        ("integer", "90", 90),
        ("integer", " 7 ", 7),
        ("integer", 12, 12),
        ("number", "90", 90),
        ("number", "0.5", 0.5),
        ("number", " .25 ", 0.25),
        ("number", 7, 7),
        ("number", 1.5, 1.5),
        ("boolean", "true", True),
        ("boolean", "False", False),
        ("boolean", True, True),
    ],
)
def test_a_value_is_read_back_into_its_type(value_type, raw, expected):
    assert parameters.coerce(value_type, raw) == expected


@pytest.mark.parametrize(
    "value_type, raw",
    [
        ("integer", "ninety"),
        ("integer", True),
        ("integer", ""),
        ("integer", "1_000"),
        ("integer", "2.5"),
        ("number", "nan"),
        ("number", "inf"),
        ("number", "1_0"),
        ("number", ""),
        ("number", True),
        ("boolean", "yes"),
    ],
)
def test_a_value_of_the_wrong_type_is_refused(value_type, raw):
    with pytest.raises(ValueError):
        parameters.coerce(value_type, raw)


def _entry():
    return parameters.parameter_defaults()[0]


def _valid_value(entry):
    """A value the entry's setting accepts: its first listed choice, or a sample.

    Every patterned text setting here accepts empty, which means "none".
    """
    declared = schema.section_schema(entry.section)["properties"][entry.setting]
    if "enum" in declared:
        return declared["enum"][0]
    if "pattern" in declared:
        return ""
    return {
        "string": "text",
        "integer": declared.get("minimum", 1),
        "number": 30,
        "boolean": True,
    }[entry.value_type]


def test_the_seeder_write_lays_each_parameter_over_the_baseline():
    entry = _entry()
    value = _valid_value(entry)
    baseline = {entry.section: {"untouched": "kept"}, "other": {"x": 1}}
    applied = parameters.apply_parameter_defaults(baseline, {entry.parameter: str(value)})
    assert applied == [entry.parameter]
    assert baseline[entry.section] == {"untouched": "kept", entry.setting: value}
    assert baseline["other"] == {"x": 1}


@pytest.mark.parametrize("entry", parameters.parameter_defaults(), ids=lambda e: e.parameter)
def test_every_setting_takes_a_value_its_schema_accepts(entry):
    baseline: dict = {}
    value = _valid_value(entry)
    assert parameters.apply_parameter_defaults(baseline, {entry.parameter: str(value)}) == [
        entry.parameter
    ]
    assert baseline[entry.section][entry.setting] == value


def test_a_value_the_schema_refuses_is_left_out():
    """A nested stack deployed on its own passes an empty model id."""
    entry = _entry()
    baseline: dict = {}
    assert parameters.apply_parameter_defaults(baseline, {entry.parameter: ""}) == []
    assert baseline == {}


def test_a_parameter_the_property_does_not_name_is_left_out():
    baseline: dict = {}
    assert parameters.apply_parameter_defaults(baseline, {}) == []
    assert baseline == {}


def test_a_name_no_setting_is_backed_by_is_ignored():
    baseline: dict = {}
    assert parameters.apply_parameter_defaults(baseline, {"NotAParameter": "x"}) == []
    assert baseline == {}


def test_a_value_that_is_not_its_type_is_left_out(monkeypatch):
    entry = parameters.ParameterDefault("SomeNumber", "language_models", "n", "integer")
    monkeypatch.setattr(parameters, "parameter_defaults", lambda: (entry,))
    baseline: dict = {}
    assert parameters.apply_parameter_defaults(baseline, {"SomeNumber": "many"}) == []
    assert baseline == {}


def test_a_reader_prefers_the_section(monkeypatch):
    entry = _entry()
    monkeypatch.setenv("SOME_ENV", "from-env")
    store = _Store({entry.section: {entry.setting: "from-store"}})
    assert parameters.read_setting(
        entry.section, entry.setting, env_var="SOME_ENV", store=store
    ) == ("from-store")


def test_a_reader_falls_back_to_the_environment_when_the_section_lacks_the_setting(monkeypatch):
    entry = _entry()
    monkeypatch.setenv("SOME_ENV", "from-env")
    store = _Store({entry.section: {}})
    assert parameters.read_setting(
        entry.section, entry.setting, env_var="SOME_ENV", store=store
    ) == ("from-env")


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetItem"),
        ConfigurationTableNotConfigured("unset"),
    ],
)
def test_a_table_that_cannot_be_read_is_the_environments_answer(monkeypatch, error):
    entry = _entry()
    monkeypatch.setenv("SOME_ENV", "from-env")
    assert (
        parameters.read_setting(
            entry.section, entry.setting, env_var="SOME_ENV", store=_Store(error=error)
        )
        == "from-env"
    )


def test_with_no_table_named_the_environment_is_the_answer(monkeypatch):
    entry = _entry()
    monkeypatch.setenv("SOME_ENV", "from-env")
    assert parameters.read_setting(entry.section, entry.setting, env_var="SOME_ENV") == "from-env"


def test_with_neither_the_reader_gets_its_default():
    entry = _entry()
    assert (
        parameters.read_setting(entry.section, entry.setting, env_var="UNSET_ENV", default="d")
        == "d"
    )


def test_a_stored_value_of_the_wrong_type_falls_back_to_the_environment(monkeypatch):
    entry = parameters.ParameterDefault("SomeNumber", "language_models", "n", "integer")
    monkeypatch.setattr(parameters, "parameter_defaults", lambda: (entry,))
    monkeypatch.setenv("SOME_ENV", "5")
    store = _Store({"language_models": {"n": "many"}})
    assert parameters.read_setting("language_models", "n", env_var="SOME_ENV", store=store) == 5


def test_an_integer_with_an_empty_environment_variable_gets_the_default(monkeypatch):
    entry = parameters.ParameterDefault("SomeNumber", "language_models", "n", "integer")
    monkeypatch.setattr(parameters, "parameter_defaults", lambda: (entry,))
    monkeypatch.setenv("SOME_ENV", "")
    assert parameters.read_setting("language_models", "n", env_var="SOME_ENV", default=3) == 3


def test_reading_a_setting_no_parameter_backs_is_refused():
    with pytest.raises(KeyError):
        parameters.read_setting("language_models", "notASetting", env_var="X")


def test_one_store_serves_every_read_in_a_process(monkeypatch):
    built = []

    class _Built(_Store):
        def __init__(self):
            super().__init__({})
            built.append(self)

    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "table")
    monkeypatch.setattr(parameters, "ConfigurationStore", _Built)
    entry = _entry()
    for _ in range(3):
        parameters.read_setting(entry.section, entry.setting, env_var="UNSET_ENV")
    assert len(built) == 1
    assert built[0].reads == 3


# ── a parameter that is the setting's upper limit ───────────────────────────

RETENTION = "retention"
MEETINGS = "meetingRecordExpirationInDays"


def test_a_limited_setting_uses_a_shorter_configured_value(monkeypatch):
    monkeypatch.setenv("LIMIT_ENV", "90")
    store = _Store({RETENTION: {MEETINGS: 30}})
    assert parameters.read_setting(RETENTION, MEETINGS, env_var="LIMIT_ENV", store=store) == 30


def test_a_limited_setting_never_exceeds_the_parameter(monkeypatch):
    """A longer value can reach the profile by a restore, or by a stack update
    lowering the parameter after it was saved; the parameter still bounds it."""
    monkeypatch.setenv("LIMIT_ENV", "7")
    store = _Store({RETENTION: {MEETINGS: 30}})
    assert parameters.read_setting(RETENTION, MEETINGS, env_var="LIMIT_ENV", store=store) == 7


def test_a_limited_setting_the_section_lacks_is_the_parameter(monkeypatch):
    monkeypatch.setenv("LIMIT_ENV", "0.5")
    store = _Store({RETENTION: {}})
    assert parameters.read_setting(RETENTION, MEETINGS, env_var="LIMIT_ENV", store=store) == 0.5


def test_a_limited_setting_that_is_not_above_zero_is_not_used(monkeypatch):
    monkeypatch.setenv("LIMIT_ENV", "90")
    store = _Store({RETENTION: {MEETINGS: 0}})
    assert parameters.read_setting(RETENTION, MEETINGS, env_var="LIMIT_ENV", store=store) == 90


def test_a_limited_setting_with_no_parameter_uses_the_section():
    store = _Store({RETENTION: {MEETINGS: 30}})
    assert parameters.read_setting(RETENTION, MEETINGS, env_var="UNSET_ENV", store=store) == 30


def test_a_limited_setting_with_neither_gets_its_default():
    assert parameters.read_setting(RETENTION, MEETINGS, env_var="UNSET_ENV", default=None) is None


def test_a_limited_setting_whose_table_cannot_be_read_is_the_parameter(monkeypatch):
    monkeypatch.setenv("LIMIT_ENV", "14")
    error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetItem")
    assert (
        parameters.read_setting(RETENTION, MEETINGS, env_var="LIMIT_ENV", store=_Store(error=error))
        == 14
    )
