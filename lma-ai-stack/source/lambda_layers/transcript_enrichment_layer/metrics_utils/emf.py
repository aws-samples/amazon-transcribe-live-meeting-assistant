# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Write application metrics as CloudWatch Embedded Metric Format records.

EMF rather than ``PutMetricData``: the record is a line of JSON on stdout that
CloudWatch Logs turns into a metric at ingestion. That costs one log line
instead of one API call, needs no IAM grant beyond the log writes the function
already does, and — the reason it matters here — cannot throttle or time out on
the hottest path in the solution, which is a transcript segment per utterance
per concurrent meeting.

The record is built by hand rather than with a library. Two reasons:

* the same shape has to come out of the Python Lambda functions and out of the
  TypeScript transcriber, which runs on ECS rather than Lambda, where the
  available libraries either assume a Lambda runtime or default to a sink that
  expects a CloudWatch agent alongside the process. One builder per language
  against the published format keeps the two records identical and adds no
  dependency to either;
* an emitter is the piece that has to work on a deployment nobody is watching,
  so its tests assert the bytes it writes. This module imports nothing outside
  the standard library, so those tests run anywhere ``pytest`` does — including
  CI, where this layer's ``requirements.txt`` is not installed.

Usage. Import the shared ``METRICS`` rather than constructing an emitter: it
accumulates for the whole invocation, which only works if every module of a
function adds to the same one, and if the single ``flush()`` in the handler is
flushing that one. A module with its own emitter is never flushed by the
handler and publishes nothing.

    from metrics_utils.emf import METRICS

    METRICS.count(catalogue.MEETINGS_STARTED)   # anywhere in the function
    ...
    METRICS.flush()                             # once, in the handler's finally

Nothing here raises. A metric that cannot be written must not take down the
pipeline it is measuring, so every failure path logs and continues; the gate in
``lma-ai-stack/test/test_main_stack_metrics.py`` is what makes sure the one
input that can silence the whole module — a missing ``METRIC_NAMESPACE`` — is
wired in the template.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Callable, Dict, List, Union

from . import catalogue

LOGGER = logging.getLogger(__name__)

Number = Union[int, float]

# CloudWatch's documented ceilings for one EMF record. Reaching either is not an
# error — the record is flushed and a new one started — but exceeding them is,
# because CloudWatch rejects the whole record and the log line it came in is
# then the only trace that anything was measured.
MAX_METRICS_PER_RECORD = 100
MAX_VALUES_PER_METRIC = 100

# EMF publishes a metric with no dimensions as one dimension set that is empty.
# A record with no `Dimensions` key at all is a different thing: CloudWatch
# accepts it and extracts nothing.
NO_DIMENSIONS: List[List[str]] = [[]]


class MetricEmitter:
    """Accumulates metrics for one invocation and writes them as one record.

    Accumulating rather than writing per call is about log volume: the Call
    Event Processor handles up to 200 Kinesis records per invocation and would
    otherwise add a line per record for a number that is only ever read as a
    sum over five minutes.

    Counts accumulate into a running total, so the ``Sum`` statistic over a
    period is the number of things that happened. Measurements such as a
    latency accumulate as a list of observations, so percentiles mean what they
    look like; summing those would be meaningless.
    """

    def __init__(
        self,
        namespace: str,
        *,
        sink: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._namespace = namespace
        self._sink = sink
        self._clock = clock
        self._counts: Dict[str, Number] = {}
        self._measurements: Dict[str, List[Number]] = {}
        # Logged once rather than per call: a function whose namespace is unset
        # would otherwise write the same line for every metric it tried to
        # emit, which is the opposite of the problem being reported.
        self._warned_disabled = False

    @classmethod
    def from_environment(cls, **kwargs) -> "MetricEmitter":
        """Build an emitter from ``METRIC_NAMESPACE``.

        An unset or blank value yields a disabled emitter rather than an
        exception. That is a deliberate choice about which failure is worse: a
        stack that stops writing transcripts because its metrics are
        misconfigured is worse than one that stops reporting metrics, and the
        template gate is what stops the misconfiguration reaching a deployment
        in the first place.
        """
        return cls(os.environ.get(catalogue.NAMESPACE_ENV_VAR, "").strip(), **kwargs)

    @property
    def enabled(self) -> bool:
        """False when no namespace is configured, in which case nothing is written."""
        return bool(self._namespace)

    @property
    def namespace(self) -> str:
        return self._namespace

    def count(self, name: str, value: Number = 1) -> None:
        """Add to a counter. ``Sum`` over a period is the number of occurrences."""
        self._add(name, value, measurement=False)

    def measure(self, name: str, value: Number) -> None:
        """Record one observation of a measurement such as a duration."""
        self._add(name, value, measurement=True)

    def _add(self, name: str, value: Number, *, measurement: bool) -> None:
        if not self.enabled:
            if not self._warned_disabled:
                self._warned_disabled = True
                LOGGER.error(
                    "%s is unset, so no application metrics are being published by this "
                    "function. Nothing downstream reports this: the widgets that read these "
                    "metrics stay empty and their alarms read OK, because both treat "
                    "missing data as not breaching.",
                    catalogue.NAMESPACE_ENV_VAR,
                )
            return

        definition = catalogue.BY_NAME.get(name)
        if definition is None:
            # Refused rather than passed through. An unlisted name would create
            # a second series next to the real one, with no widget and no alarm
            # reading it, and nothing would ever say so.
            LOGGER.error(
                "refusing to emit %r: not in metrics_utils.catalogue, so no widget or "
                "alarm reads it. Add it to the catalogue first.",
                name,
            )
            return

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # bool is an int subclass, and True would silently become 1 under a
            # name whose unit is Milliseconds.
            LOGGER.error("refusing to emit %r with non-numeric value %r", name, value)
            return

        if not math.isfinite(value):
            # `json.dumps` writes NaN and Infinity as bare `NaN`/`Infinity`,
            # which is not JSON. CloudWatch cannot parse the record at all, so
            # one such value loses every metric accumulated alongside it rather
            # than just itself. A duration computed from two clock readings is
            # the realistic way to get one.
            LOGGER.error("refusing to emit %r with non-finite value %r", name, value)
            return

        if measurement:
            values = self._measurements.setdefault(name, [])
            values.append(value)
            if len(values) >= MAX_VALUES_PER_METRIC:
                self.flush()
        else:
            self._counts[name] = self._counts.get(name, 0) + value

        if self._metric_count() >= MAX_METRICS_PER_RECORD:
            self.flush()

    def _metric_count(self) -> int:
        return len(self._counts) + len(self._measurements)

    def build_record(self) -> Union[dict, None]:
        """The EMF record for what has accumulated, or None if nothing has.

        Separate from :meth:`flush` so the shape can be asserted directly.
        """
        if not self.enabled or not self._metric_count():
            return None

        definitions = []
        body: Dict[str, Union[Number, List[Number]]] = {}
        for name, total in self._counts.items():
            definitions.append({"Name": name, "Unit": catalogue.BY_NAME[name].unit})
            body[name] = total
        for name, values in self._measurements.items():
            definitions.append({"Name": name, "Unit": catalogue.BY_NAME[name].unit})
            body[name] = list(values)

        return {
            "_aws": {
                # Milliseconds since the epoch, as an integer. A float here is
                # rejected and the record is dropped.
                "Timestamp": int(self._clock() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": NO_DIMENSIONS,
                        "Metrics": definitions,
                    }
                ],
            },
            **body,
        }

    def flush(self) -> None:
        """Write the accumulated metrics as one record and start a new one.

        Safe to call when nothing has accumulated, and safe to call twice: the
        accumulators are cleared whether or not the write succeeded, so a sink
        that fails loses one record rather than repeating it for the rest of
        the invocation.
        """
        record = self.build_record()
        self._counts = {}
        self._measurements = {}
        if record is None:
            return
        try:
            self._sink(json.dumps(record, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - see the module docstring
            LOGGER.exception("could not write an application metric record")


#: The emitter the Lambda functions use, built once per container from the
#: environment, in the same way and for the same reason as this layer's
#: module-level loggers and boto3 clients. Two modules of the same function
#: import this name and must share one accumulator, so that the single
#: ``flush()`` in the handler writes everything the invocation measured.
#:
#: Tests do not use this instance — they construct a ``MetricEmitter`` with
#: their own sink and clock, so nothing depends on the ambient environment.
METRICS = MetricEmitter.from_environment()
