/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Write application metrics as CloudWatch Embedded Metric Format records.
 *
 * The same approach, and the same record shape, as the WebSocket transcriber's
 * emitter (`lma-websocket-transcriber-stack/source/app/src/metrics/emf.ts`):
 * a line of JSON on stdout that CloudWatch Logs turns into a metric at
 * ingestion. The VP's stdout reaches CloudWatch unchanged on every launch type
 * -- the ECS container's `awslogs` driver, and on a MicroVM the supervisor
 * spawns the app with inherited stdio -- so there is no API call on a
 * meeting's path and no `cloudwatch:PutMetricData` grant on the task role.
 *
 * Written straight to stdout rather than through `console.log`, so that a
 * test or anything else that wraps the console cannot turn the record into
 * something that is not JSON. Records are written immediately: a VP is one
 * long-running process, the metrics here are rare, and the process can be
 * stopped at any moment by the meeting ending.
 */

import * as catalogue from './metrics-catalogue.js';

export type MetricSink = (line: string) => void;

const defaultSink: MetricSink = (line: string) => {
    process.stdout.write(`${line}\n`);
};

export class MetricEmitter {
    private readonly namespace: string;

    private readonly sink: MetricSink;

    private readonly clock: () => number;

    private warnedDisabled = false;

    constructor(namespace: string, options: { sink?: MetricSink; clock?: () => number } = {}) {
        this.namespace = namespace.trim();
        this.sink = options.sink ?? defaultSink;
        this.clock = options.clock ?? Date.now;
    }

    /**
     * Build an emitter from `METRIC_NAMESPACE`. An unset or blank value yields
     * a disabled emitter rather than throwing: a VP that refused to join a
     * meeting because its metrics were misconfigured would be the worse
     * outcome. The template gate keeps the misconfiguration out of a deployment.
     */
    static fromEnvironment(options: { sink?: MetricSink } = {}): MetricEmitter {
        return new MetricEmitter(process.env[catalogue.METRIC_NAMESPACE_ENV_VAR] ?? '', options);
    }

    get enabled(): boolean {
        return this.namespace.length > 0;
    }

    /** Add to a counter. `Sum` over a period is the number of occurrences. */
    count(name: string, value = 1): void {
        if (!this.enabled) {
            if (!this.warnedDisabled) {
                this.warnedDisabled = true;
                console.warn(
                    `${catalogue.METRIC_NAMESPACE_ENV_VAR} is unset, so this Virtual Participant `
                    + 'publishes no application metrics.',
                );
            }
            return;
        }
        const unit = catalogue.UNIT_BY_NAME[name];
        if (unit === undefined) {
            // Refused rather than passed through: an unlisted name would create
            // a series that no widget reads.
            console.error(`refusing to emit metric '${name}': not in src/metrics-catalogue.ts`);
            return;
        }
        if (!Number.isFinite(value)) {
            console.error(`refusing to emit metric '${name}' with non-finite value ${value}`);
            return;
        }
        const record = {
            _aws: {
                Timestamp: Math.round(this.clock()),
                CloudWatchMetrics: [
                    {
                        Namespace: this.namespace,
                        // `[[]]`, not `[]`: one empty dimension set, which is
                        // what publishes a metric with no dimensions.
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
            console.error(`could not write an application metric record: ${String(error)}`);
        }
    }
}

/** The emitter the VP uses, built once from the environment at startup. */
export const METRICS = MetricEmitter.fromEnvironment();
