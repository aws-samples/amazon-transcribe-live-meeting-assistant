/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The metrics the Virtual Participant publishes.
 *
 * The TypeScript copy of the VP's part of one catalogue. The authoritative
 * copy, which also lists what the Lambda functions and the WebSocket
 * transcriber publish, is
 * `lma-ai-stack/source/lambda_layers/transcript_enrichment_layer/metrics_utils/catalogue.py`,
 * and a gate in `lma-ai-stack/test/test_main_stack_metrics.py` fails when the
 * two disagree -- the same arrangement the transcriber has.
 *
 * The environment variable name is the same in every component: the namespace
 * is how the dashboard and an operator find the same series.
 */

export const METRIC_NAMESPACE_ENV_VAR = 'METRIC_NAMESPACE';

/** The CloudWatch units these metrics use. */
export type MetricUnit = 'Count';

export interface MetricDefinition {
    name: string;
    unit: MetricUnit;
}

/**
 * Amazon Bedrock refused to open a Nova Sonic stream because the account's
 * concurrent-stream quota for the model was reached in that region. One per
 * refused attempt, whatever was opening the stream: the assistant's session,
 * a translator's next turn, or the pre-rendered lookup acknowledgement.
 */
export const NOVA_SONIC_STREAMS_THROTTLED = 'NovaSonicStreamsThrottled';

export const CATALOGUE: readonly MetricDefinition[] = [
    { name: NOVA_SONIC_STREAMS_THROTTLED, unit: 'Count' },
];

export const UNIT_BY_NAME: Readonly<Record<string, MetricUnit>> = Object.fromEntries(
    CATALOGUE.map((metric) => [metric.name, metric.unit]),
);
