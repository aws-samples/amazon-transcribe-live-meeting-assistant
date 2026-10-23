// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * The metrics the WebSocket transcriber publishes.
 *
 * This is the TypeScript half of one catalogue. The authoritative copy, which
 * also lists what the Python Lambda functions publish, is
 * `lma-ai-stack/source/lambda_layers/transcript_enrichment_layer/metrics_utils/catalogue.py`,
 * and a gate in `lma-ai-stack/test/test_main_stack_metrics.py` fails when the
 * two disagree. The duplication is deliberate: the alternative is generating
 * one from the other at build time, which puts a code generator between a
 * three-name list and the service that emits it.
 *
 * The environment variable name is the same in both languages and in every
 * component, for the reason given in the Python copy: the namespace is how a
 * dashboard, an alarm and an operator all find the same series.
 */

export const METRIC_NAMESPACE_ENV_VAR = 'METRIC_NAMESPACE';

/** The CloudWatch units these metrics use. */
export type MetricUnit = 'Count' | 'Milliseconds';

export interface MetricDefinition {
    name: string;
    unit: MetricUnit;
}

/** A meeting's audio session opened. The denominator for the two below. */
export const STREAMING_SESSIONS_STARTED = 'StreamingSessionsStarted';

/**
 * Milliseconds from a meeting's audio session opening to its first transcript
 * segment reaching Kinesis. Dominated by when somebody first speaks rather
 * than by pipeline latency, so it is graphed and not alarmed on.
 */
export const TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS = 'TimeToFirstTranscriptSegmentMs';

/**
 * A session closed having produced no transcript segment at all. Nothing else
 * reports this: the session closes normally and the meeting page shows an empty
 * transcript with no error anywhere.
 */
export const STREAMING_SESSIONS_WITHOUT_TRANSCRIPT = 'StreamingSessionsWithoutTranscript';

export const CATALOGUE: readonly MetricDefinition[] = [
    { name: STREAMING_SESSIONS_STARTED, unit: 'Count' },
    { name: TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS, unit: 'Milliseconds' },
    { name: STREAMING_SESSIONS_WITHOUT_TRANSCRIPT, unit: 'Count' },
];

export const UNIT_BY_NAME: Readonly<Record<string, MetricUnit>> = Object.fromEntries(
    CATALOGUE.map((metric) => [metric.name, metric.unit])
);
