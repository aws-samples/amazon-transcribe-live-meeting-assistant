# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the Embedded Metric Format records LMA writes.

These assert the record itself rather than a mock of a metrics library, because
the record *is* the contract: CloudWatch Logs parses it at ingestion and, when
it does not match the format, publishes nothing and says nothing. The only
symptom is a widget that stays empty and an alarm that stays in
INSUFFICIENT_DATA, which is indistinguishable from a quiet deployment.

Every constraint checked below is quoted from the embedded metric format
specification, because each one, if broken, causes CloudWatch to drop either
the metric or the whole record:

* the root node MUST contain `_aws`, which MUST contain `Timestamp` and
  `CloudWatchMetrics`; `Timestamp` MUST be an integer of milliseconds since the
  epoch, and a float there is rejected;
* a MetricDirective MUST contain `Namespace`, `Dimensions` and `Metrics` — all
  three, so omitting `Dimensions` for a metric that has none drops the record;
* the `Dimensions` array MUST have at least one DimensionSet, and a DimensionSet
  MAY be empty. Those two together are why publishing without dimensions is
  `[[]]` and not `[]`: the latter has no DimensionSet at all;
* a metric target MUST be a number or an array of numbers, and such an array
  MUST NOT have more than 100 members;
* a log event MUST NOT carry more than 100 MetricDefinitions.

Also asserted: the emitter is silent, and loudly so, when `METRIC_NAMESPACE` is
unset — the one misconfiguration that stops every metric in a component without
failing anything.

No AWS calls and no third-party imports: `metrics_utils.emf` deliberately
depends on nothing outside the standard library.
"""

from __future__ import annotations

import json
import logging

import pytest

from metrics_utils import catalogue
from metrics_utils.emf import (
    MAX_METRICS_PER_RECORD,
    MAX_VALUES_PER_METRIC,
    MetricEmitter,
)

NAMESPACE = "lma-test-stack"

# A name of each kind, taken from the catalogue rather than written out, so a
# rename there fails here instead of leaving these testing a name nobody emits.
A_COUNT = catalogue.MEETINGS_STARTED
A_MEASUREMENT = catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS


class Sink:
    """Collects the lines an emitter writes, and parses them on demand."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]

    @property
    def only(self) -> dict:
        assert len(self.records) == 1, f"expected exactly one record, got {len(self.records)}"
        return self.records[0]


def emitter(sink: Sink, namespace: str = NAMESPACE, now: float = 1_700_000_000.5):
    return MetricEmitter(namespace, sink=sink, clock=lambda: now)


def directive(record: dict) -> dict:
    return record["_aws"]["CloudWatchMetrics"][0]


# ── the record's shape ──────────────────────────────────────────────────────


def test_a_flushed_record_is_one_line_of_parseable_json():
    """CloudWatch parses a log event as a whole, so a record split over two
    lines, or carrying anything outside the JSON object, is not read at all."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT)
    metrics.flush()

    assert len(sink.lines) == 1
    assert "\n" not in sink.lines[0]
    assert json.loads(sink.lines[0])  # raises if it is not a JSON object


def test_the_record_carries_the_members_cloudwatch_requires():
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT)
    metrics.flush()

    record = sink.only
    assert set(record["_aws"]) == {"Timestamp", "CloudWatchMetrics"}
    assert {"Namespace", "Dimensions", "Metrics"} <= set(directive(record))


def test_the_timestamp_is_an_integer_of_milliseconds():
    """The format requires an integer. A float is rejected and the record is
    dropped, so this asserts the type and not merely the magnitude."""
    sink = Sink()
    # A clock returning a fractional second is the case that would produce a
    # float if the conversion were a bare multiplication.
    metrics = emitter(sink, now=1_700_000_000.5)
    metrics.count(A_COUNT)
    metrics.flush()

    timestamp = sink.only["_aws"]["Timestamp"]
    assert isinstance(timestamp, int)
    assert not isinstance(timestamp, bool)
    assert timestamp == 1_700_000_000_500


def test_the_namespace_is_the_one_the_emitter_was_given():
    """The whole per-stack scheme rests on this one string: the dashboard and
    the alarms select on it, so a metric published under any other namespace is
    invisible to both while looking perfectly healthy in the log."""
    sink = Sink()
    metrics = emitter(sink, namespace="some-other-stack")
    metrics.count(A_COUNT)
    metrics.flush()

    assert directive(sink.only)["Namespace"] == "some-other-stack"


def test_a_metric_with_no_dimensions_is_one_empty_dimension_set():
    """`[[]]`, not `[]`. The outer array must hold at least one DimensionSet and
    a DimensionSet may be empty, so `[]` means "no dimension sets" and
    publishes nothing, while `[[]]` means "one set, with no keys"."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT)
    metrics.flush()

    assert directive(sink.only)["Dimensions"] == [[]]


def test_every_named_metric_has_a_target_member_on_the_root_node():
    """A MetricDefinition is a reference; without the matching root member the
    metric is named but has no value, and CloudWatch publishes nothing."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT, 3)
    metrics.measure(A_MEASUREMENT, 1500)
    metrics.flush()

    record = sink.only
    for definition in directive(record)["Metrics"]:
        assert definition["Name"] in record, f"{definition['Name']} has no value on the root node"
        value = record[definition["Name"]]
        assert isinstance(value, (int, float, list))


def test_each_metric_carries_the_unit_the_catalogue_gives_it():
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT)
    metrics.measure(A_MEASUREMENT, 1500)
    metrics.flush()

    units = {d["Name"]: d["Unit"] for d in directive(sink.only)["Metrics"]}
    assert units[A_COUNT] == "Count"
    assert units[A_MEASUREMENT] == "Milliseconds"


# ── counts versus measurements ──────────────────────────────────────────────


def test_counts_accumulate_into_one_total():
    """Accumulating rather than writing per call is what keeps the Call Event
    Processor from adding a log line per Kinesis record for a number only ever
    read as a sum. The total must be the sum of the calls."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT)
    metrics.count(A_COUNT)
    metrics.count(A_COUNT, 5)
    metrics.flush()

    assert sink.only[A_COUNT] == 7


def test_measurements_accumulate_as_separate_observations():
    """A latency must stay a list. Summing observations would make the average
    and the percentiles mean nothing, and the error would look plausible."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.measure(A_MEASUREMENT, 100)
    metrics.measure(A_MEASUREMENT, 300)
    metrics.flush()

    assert sink.only[A_MEASUREMENT] == [100, 300]


def test_flushing_clears_what_was_accumulated():
    """Otherwise a second flush republishes the first flush's numbers, which for
    a counter silently doubles it."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.count(A_COUNT)
    metrics.flush()
    metrics.count(A_COUNT)
    metrics.flush()

    assert [record[A_COUNT] for record in sink.records] == [1, 1]


def test_flushing_with_nothing_accumulated_writes_nothing():
    """The handler flushes unconditionally, and most invocations emit no
    lifecycle metric at all. An empty record would be a log line per invocation
    for no information, and the format has no valid empty form."""
    sink = Sink()
    metrics = emitter(sink)
    metrics.flush()
    metrics.flush()

    assert sink.lines == []


# ── the format's ceilings ───────────────────────────────────────────────────


def test_a_measurement_flushes_before_exceeding_the_value_limit():
    """A numeric array of more than 100 members makes CloudWatch extract
    nothing from the whole log event, losing the other metrics in it too."""
    sink = Sink()
    metrics = emitter(sink)
    for value in range(MAX_VALUES_PER_METRIC + 10):
        metrics.measure(A_MEASUREMENT, value)
    metrics.flush()

    assert sink.records, "nothing was written at all"
    for record in sink.records:
        assert len(record[A_MEASUREMENT]) <= MAX_VALUES_PER_METRIC
    written = sum(len(record[A_MEASUREMENT]) for record in sink.records)
    assert written == MAX_VALUES_PER_METRIC + 10, "observations were lost rather than split"


def test_no_record_declares_more_metrics_than_the_format_allows():
    """More than 100 MetricDefinitions and CloudWatch extracts none of them.

    The catalogue is well under the limit today, so this asserts the emitter's
    behaviour against a synthetic catalogue rather than the real one — the point
    is that growing the catalogue past 100 splits records instead of silently
    publishing nothing.
    """
    sink = Sink()
    metrics = emitter(sink)
    names = [f"Synthetic{index}" for index in range(MAX_METRICS_PER_RECORD + 5)]
    extra = {
        name: catalogue.MetricDefinition(
            name=name,
            unit="Count",
            component=catalogue.Component.CALL_EVENT_PROCESSOR,
            records_swallowed_failure=False,
            meaning="synthetic",
        )
        for name in names
    }
    original = dict(catalogue.BY_NAME)
    catalogue.BY_NAME.update(extra)
    try:
        for name in names:
            metrics.count(name)
        metrics.flush()
    finally:
        catalogue.BY_NAME.clear()
        catalogue.BY_NAME.update(original)

    assert sink.records
    for record in sink.records:
        assert len(directive(record)["Metrics"]) <= MAX_METRICS_PER_RECORD


# ── refusing what would publish a series nobody reads ───────────────────────


def test_a_name_outside_the_catalogue_is_not_emitted(caplog):
    """A typo at a call site would otherwise create a near-identical second
    series, with no widget and no alarm on it, and nothing would report that."""
    sink = Sink()
    metrics = emitter(sink)
    with caplog.at_level(logging.ERROR):
        metrics.count("MeetingsStartedd")
    metrics.flush()

    assert sink.lines == []
    assert "MeetingsStartedd" in caplog.text


@pytest.mark.parametrize("value", [None, "1", True, False, [1]])
def test_a_non_numeric_value_is_not_emitted(value, caplog):
    """A metric target must be numeric. `True` is the dangerous one: bool is a
    subclass of int, so it would pass a naive check and publish as 1."""
    sink = Sink()
    metrics = emitter(sink)
    with caplog.at_level(logging.ERROR):
        metrics.measure(A_MEASUREMENT, value)
    metrics.flush()

    assert sink.lines == []


# ── the misconfiguration this whole increment is about ──────────────────────


def test_an_emitter_with_no_namespace_publishes_nothing_and_says_so(caplog):
    """The failure this is all built to prevent.

    A component whose namespace is unset keeps working perfectly and silently
    stops being measured. The emitter cannot fix that, so it must at least be
    loud about it — and the template gate is what stops it reaching a deploy.
    """
    sink = Sink()
    metrics = emitter(sink, namespace="")
    assert not metrics.enabled

    with caplog.at_level(logging.ERROR):
        metrics.count(A_COUNT)
        metrics.measure(A_MEASUREMENT, 10)
    metrics.flush()

    assert sink.lines == []
    assert catalogue.NAMESPACE_ENV_VAR in caplog.text


def test_the_missing_namespace_is_reported_once_not_per_metric(caplog):
    """Otherwise the report drowns in itself, one line per attempted metric."""
    sink = Sink()
    metrics = emitter(sink, namespace="")
    with caplog.at_level(logging.ERROR):
        for _ in range(20):
            metrics.count(A_COUNT)

    complaints = [r for r in caplog.records if catalogue.NAMESPACE_ENV_VAR in r.getMessage()]
    assert len(complaints) == 1


def test_from_environment_reads_the_namespace_variable(monkeypatch):
    monkeypatch.setenv(catalogue.NAMESPACE_ENV_VAR, "lma-from-env")
    assert MetricEmitter.from_environment().namespace == "lma-from-env"


@pytest.mark.parametrize("configured", ["", "   "])
def test_from_environment_treats_a_blank_namespace_as_unset(monkeypatch, configured):
    """`!Ref` of a parameter that was left empty renders as an empty string, so
    blank has to be handled as absent rather than published as a namespace."""
    monkeypatch.setenv(catalogue.NAMESPACE_ENV_VAR, configured)
    assert not MetricEmitter.from_environment().enabled


def test_from_environment_with_the_variable_absent_is_disabled(monkeypatch):
    monkeypatch.delenv(catalogue.NAMESPACE_ENV_VAR, raising=False)
    assert not MetricEmitter.from_environment().enabled


# ── never breaking the thing being measured ─────────────────────────────────


def test_a_failing_sink_does_not_raise_and_does_not_repeat(caplog):
    """Measuring the transcript pipeline must not be able to stop it. A sink
    that fails loses its own record and nothing else."""
    calls = []

    def broken(line: str) -> None:
        calls.append(line)
        raise OSError("stdout is gone")

    metrics = MetricEmitter(NAMESPACE, sink=broken, clock=lambda: 0.0)
    metrics.count(A_COUNT)
    with caplog.at_level(logging.ERROR):
        metrics.flush()  # must not raise
        metrics.flush()

    assert len(calls) == 1, "the failed record was retried instead of dropped"
