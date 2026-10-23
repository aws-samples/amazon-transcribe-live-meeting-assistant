# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the metric catalogue's own consistency.

The catalogue is read by the emitter, by the Lambda functions and by a static
gate over `lma-main.yaml`, so a mistake in it propagates into all three. What is
checked here is only what the catalogue can be wrong about by itself; whether a
metric reaches a widget or an alarm is checked by
`lma-ai-stack/test/test_main_stack_metrics.py`, which can see the template.

The unit check is the load-bearing one. CloudWatch rejects an unrecognised unit
per record at ingestion, and the only symptom is a series that never appears —
so a typo in a unit string is exactly as invisible as a typo in a metric name,
and far easier to make.
"""

from __future__ import annotations

import re

import pytest

from metrics_utils import catalogue

# From the embedded metric format specification: a metric name must be 1 to 255
# printable ASCII characters and must not be only whitespace.
PRINTABLE_ASCII = re.compile(r"^[ -~]*[!-~][ -~]*$")


def test_the_catalogue_is_not_empty():
    """Guards every other test here, each of which iterates over it."""
    assert len(catalogue.CATALOGUE) >= 5


def test_every_unit_is_one_cloudwatch_accepts():
    wrong = {
        metric.name: metric.unit
        for metric in catalogue.CATALOGUE
        if metric.unit not in catalogue.CLOUDWATCH_UNITS
    }
    assert not wrong, (
        f"these units are not CloudWatch units, so the records carrying them are rejected "
        f"at ingestion and the series never appears: {wrong}"
    )


def test_metric_names_are_unique():
    names = [metric.name for metric in catalogue.CATALOGUE]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate metric names: {duplicates}"


@pytest.mark.parametrize("metric", catalogue.CATALOGUE, ids=lambda m: m.name)
def test_every_metric_name_is_one_cloudwatch_accepts(metric):
    assert 1 <= len(metric.name) <= 255
    assert PRINTABLE_ASCII.match(metric.name), (
        f"{metric.name!r} is not 1 to 255 printable ASCII characters"
    )


@pytest.mark.parametrize("metric", catalogue.CATALOGUE, ids=lambda m: m.name)
def test_every_metric_says_what_it_means(metric):
    """The catalogue entry is where the docs and the dashboard titles come
    from, so an entry with no explanation produces a widget nobody can read."""
    assert len(metric.meaning.split()) >= 10, f"{metric.name} has no usable description"


@pytest.mark.parametrize("metric", catalogue.CATALOGUE, ids=lambda m: m.name)
def test_a_millisecond_metric_is_named_so_a_reader_knows_the_unit(metric):
    """The unit is not shown on an alarm notification or in a metric-math
    expression, so a duration whose name does not carry it gets compared
    against a threshold in whatever unit the reader assumed."""
    if metric.unit == "Milliseconds":
        assert metric.name.endswith("Ms"), f"{metric.name} is in milliseconds but does not say so"
    else:
        assert not metric.name.endswith("Ms"), f"{metric.name} reads as milliseconds but is not"


def test_by_name_covers_the_whole_catalogue():
    assert set(catalogue.BY_NAME) == {metric.name for metric in catalogue.CATALOGUE}


def test_every_component_publishes_something():
    """A component in the enum with no metrics means either a dead enum member
    or an emitter somebody forgot to list."""
    for component in catalogue.Component:
        assert catalogue.names_for(component), f"{component.value} publishes no metric"


def test_the_metrics_that_need_an_alarm_are_marked_and_are_counts():
    """These record a condition the code otherwise swallows.

    Each must be a count, because the alarm on it asks "did this happen at all",
    which needs a `Sum` of occurrences rather than a measurement.
    """
    needs_alarm = catalogue.must_be_alarmed()
    assert needs_alarm, "nothing is marked as recording a swallowed failure, which cannot be right"
    for name in needs_alarm:
        assert catalogue.BY_NAME[name].unit == "Count"


def test_the_namespace_variable_is_the_name_the_templates_set():
    """Spelled out rather than referenced, so that renaming it in the catalogue
    without renaming it in the templates fails here. The templates are checked
    against this same constant by the gate in lma-ai-stack/test/."""
    assert catalogue.NAMESPACE_ENV_VAR == "METRIC_NAMESPACE"
