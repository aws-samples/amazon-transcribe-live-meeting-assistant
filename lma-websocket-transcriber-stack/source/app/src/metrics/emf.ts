// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * Write application metrics as CloudWatch Embedded Metric Format records.
 *
 * EMF rather than `PutMetricData`: the record is a line of JSON on stdout that
 * CloudWatch Logs turns into a metric at ingestion. The container's log driver
 * is already `awslogs`, so there is nothing to configure, no API call on a
 * meeting's path, and no `cloudwatch:PutMetricData` grant on the task role --
 * CloudWatch documents that EMF needs only the log-writing permission the task
 * already has.
 *
 * **The record must not go through the server's logger.** The Fastify pino
 * instance is configured with a `pino-pretty` transport, so everything written
 * through `server.log` reaches CloudWatch as a human-readable line rather than
 * JSON, and CloudWatch extracts metrics only from JSON. This module therefore
 * writes to stdout directly. A record is a few hundred bytes, comfortably under
 * the 4 KB a single pipe write is atomic for on Linux, so it cannot be
 * interleaved with a line the pretty transport is writing from its own worker.
 *
 * Unlike the Python emitter, which accumulates for an invocation and flushes
 * once, this one writes immediately. There is no invocation boundary in a
 * long-lived server to flush at; these metrics are emitted once per session
 * rather than per record, so the log volume is the same either way; and the
 * process can be replaced by ECS without `endCall` running, which would lose
 * whatever an accumulator was holding.
 */

import * as catalogue from './catalogue';

export type MetricSink = (line: string) => void;

/** From the EMF specification: a log event may not declare more than this. */
export const MAX_METRICS_PER_RECORD = 100;

const defaultSink: MetricSink = (line: string) => {
    process.stdout.write(`${line}\n`);
};

/**
 * Somewhere to report a refused or failed emission.
 *
 * Narrower than `FastifyInstance` on purpose, so the emitter can be constructed
 * in a test with a two-line object, and so nothing here can accidentally route
 * a metric record itself through the pretty-printed logger.
 */
export interface MetricLog {
    error(message: string): void;
}

const consoleLog: MetricLog = {
    error: (message: string) => {
        console.error(message);
    },
};

export class MetricEmitter {
    private readonly namespace: string;

    private readonly sink: MetricSink;

    private readonly clock: () => number;

    private readonly log: MetricLog;

    private warnedDisabled = false;

    constructor(
        namespace: string,
        options: { sink?: MetricSink; clock?: () => number; log?: MetricLog } = {}
    ) {
        this.namespace = namespace.trim();
        this.sink = options.sink ?? defaultSink;
        this.clock = options.clock ?? Date.now;
        this.log = options.log ?? consoleLog;
    }

    /**
     * Build an emitter from `METRIC_NAMESPACE`.
     *
     * An unset or blank value yields a disabled emitter rather than throwing.
     * A transcriber that refused to start because its metrics were
     * misconfigured would be a worse outcome than one that is not measured;
     * the template gate in `lma-ai-stack/test/test_main_stack_metrics.py` is
     * what keeps the misconfiguration out of a deployment.
     */
    static fromEnvironment(options: { sink?: MetricSink; log?: MetricLog } = {}): MetricEmitter {
        return new MetricEmitter(process.env[catalogue.METRIC_NAMESPACE_ENV_VAR] ?? '', options);
    }

    get enabled(): boolean {
        return this.namespace.length > 0;
    }

    /** Add to a counter. `Sum` over a period is the number of occurrences. */
    count(name: string, value = 1): void {
        this.write(name, value);
    }

    /** Record one observation of a measurement such as a duration. */
    measure(name: string, value: number): void {
        this.write(name, value);
    }

    private write(name: string, value: number): void {
        if (!this.enabled) {
            if (!this.warnedDisabled) {
                this.warnedDisabled = true;
                this.log.error(
                    `${catalogue.METRIC_NAMESPACE_ENV_VAR} is unset, so this task publishes no ` +
                        'application metrics. Nothing downstream reports that: the widgets that ' +
                        'read them stay empty and the alarms stay in INSUFFICIENT_DATA.'
                );
            }
            return;
        }

        const unit = catalogue.UNIT_BY_NAME[name];
        if (unit === undefined) {
            // Refused rather than passed through: an unlisted name would create
            // a second series next to the real one, which no widget and no
            // alarm reads and nothing reports.
            this.log.error(
                `refusing to emit metric '${name}': not in src/metrics/catalogue.ts, so no ` +
                    'widget or alarm reads it. Add it to the catalogue first.'
            );
            return;
        }

        if (!Number.isFinite(value)) {
            // NaN and Infinity both serialise to `null` in JSON, which makes
            // the metric target non-numeric and drops the record.
            this.log.error(`refusing to emit metric '${name}' with non-finite value ${value}`);
            return;
        }

        const record = {
            _aws: {
                // Milliseconds since the epoch, as an integer. The format
                // rejects a float and drops the record.
                Timestamp: Math.round(this.clock()),
                CloudWatchMetrics: [
                    {
                        Namespace: this.namespace,
                        // `[[]]`, not `[]`: the outer array must hold at least
                        // one DimensionSet and a DimensionSet may be empty, so
                        // `[]` means "no dimension sets" and publishes nothing.
                        Dimensions: [[]],
                        Metrics: [{ Name: name, Unit: unit }],
                    },
                ],
            },
            [name]: value,
        };

        try {
            this.sink(JSON.stringify(record));
        } catch (error) {
            // Measuring a meeting must never be able to interrupt it.
            this.log.error(`could not write an application metric record: ${String(error)}`);
        }
    }
}

/** The emitter the server uses, built once from the environment at startup. */
export const METRICS = MetricEmitter.fromEnvironment();
