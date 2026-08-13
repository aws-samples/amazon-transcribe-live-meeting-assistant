/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Runtime configuration for the MicroVM ASR engine.
 *
 * The diarization operating point is empirical and specific to the speaker model:
 * the shipped threshold was measured on real meeting audio, and re-measuring it for
 * another embedder, language or microphone should not require a 20-minute stack
 * update. So every knob is an optional override in DynamoDB, edited from the ASR
 * Config admin page and read here at meeting start.
 *
 * Overrides only. An absent table, an absent record or a blank field all mean "use
 * the CloudFormation parameter", so there is no default record to keep in sync and
 * no failure mode where an unreachable table changes behaviour.
 */
import { FastifyInstance } from 'fastify';
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';

import { normalizeErrorForLogging } from '../utils/common';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const ASR_CONFIG_TABLE_NAME = process.env['ASR_CONFIG_TABLE_NAME'] || '';
const ASR_CONFIG_ID = 'CustomAsrConfig';
// Re-read at most this often. A meeting picks up a change on its next start, which
// is the tuning loop this exists to serve; caching keeps a burst of concurrent
// meetings from each doing their own GetItem.
const ASR_CONFIG_TTL_MS = parseInt(process.env['ASR_CONFIG_TTL_MS'] || '30000', 10);

const dynamoClient = new DynamoDBClient({ region: AWS_REGION });

export interface AsrRuntimeConfig {
    speakerThreshold: number;
    maxSpeakers: number;
    endpointingMs: number;
    minSegmentMs?: number;
    requireCorroboration?: boolean;
    diarizeByDefault: boolean;
    engineDefaultMicrovm: boolean;
    /**
     * Whether an admin set the threshold for this deployment, rather than it coming
     * from the stack parameter. For an unmeasured speaker model that is the only
     * evidence the operating point was ever checked against real audio, so it is
     * what lets diarization run at all.
     */
    speakerThresholdOverridden: boolean;
}

const envDefaults = (): AsrRuntimeConfig => ({
    speakerThreshold: parseFloat(process.env['ASR_SPEAKER_THRESHOLD'] || '0.2'),
    maxSpeakers: parseInt(process.env['ASR_MAX_SPEAKERS'] || '0', 10),
    endpointingMs: parseInt(process.env['ASR_ENDPOINTING_MS'] || '1200', 10),
    // Undefined means "whatever the image was built with" — these two only reached
    // the wire protocol recently, so not sending them is a valid state.
    minSegmentMs: process.env['ASR_MIN_SEGMENT_MS']
        ? parseInt(process.env['ASR_MIN_SEGMENT_MS'], 10)
        : undefined,
    requireCorroboration: process.env['ASR_REQUIRE_CORROBORATION']
        ? process.env['ASR_REQUIRE_CORROBORATION'] === 'true'
        : undefined,
    diarizeByDefault: (process.env['ASR_DIARIZE_DEFAULT'] || 'true') === 'true',
    engineDefaultMicrovm: (process.env['ASR_ENGINE_DEFAULT'] || 'transcribe').toLowerCase() === 'microvm',
    speakerThresholdOverridden: false,
});

/**
 * Whether anyone has measured this speaker model's operating point.
 *
 * Set to "false" by the stack when the embedder is customer-supplied or has no
 * measurement in the catalog. Defaults to true when unset so a local run or an
 * older deployment behaves as before.
 */
export const isSpeakerModelMeasured = (): boolean =>
    (process.env['ASR_SPEAKER_MODEL_MEASURED'] || 'true') !== 'false';

let cached: { config: AsrRuntimeConfig; at: number } | undefined;

/** Exposed for tests: forget the cached record. */
export const resetAsrConfigCache = (): void => {
    cached = undefined;
};

const numberOverride = (raw: string | undefined, fallback: number): number => {
    if (raw === undefined || raw.trim() === '') {
        return fallback;
    }
    const value = Number(raw);
    return Number.isFinite(value) ? value : fallback;
};

/**
 * Effective config for a meeting: DynamoDB overrides layered over the env defaults.
 *
 * Never throws. A failed read logs and returns the env defaults, because losing the
 * ability to transcribe over a config lookup would be a far worse failure than
 * running with the deployment's own parameters.
 */
export const getAsrRuntimeConfig = async (
    server: FastifyInstance
): Promise<AsrRuntimeConfig> => {
    const defaults = envDefaults();
    if (!ASR_CONFIG_TABLE_NAME) {
        return defaults;
    }
    if (cached && Date.now() - cached.at < ASR_CONFIG_TTL_MS) {
        return cached.config;
    }

    try {
        const result = await dynamoClient.send(
            new GetItemCommand({
                TableName: ASR_CONFIG_TABLE_NAME,
                Key: { AsrConfigId: { S: ASR_CONFIG_ID } },
            })
        );
        const item = result.Item;
        const config: AsrRuntimeConfig = item
            ? {
                speakerThreshold: numberOverride(item['speakerThreshold']?.S, defaults.speakerThreshold),
                maxSpeakers: numberOverride(item['maxSpeakers']?.S, defaults.maxSpeakers),
                endpointingMs: numberOverride(item['endpointingMs']?.S, defaults.endpointingMs),
                minSegmentMs:
                    item['minSegmentMs']?.S && item['minSegmentMs'].S.trim() !== ''
                        ? Number(item['minSegmentMs'].S)
                        : defaults.minSegmentMs,
                requireCorroboration:
                    item['requireCorroboration']?.BOOL ?? defaults.requireCorroboration,
                diarizeByDefault: item['diarizeByDefault']?.BOOL ?? defaults.diarizeByDefault,
                engineDefaultMicrovm:
                    item['engineDefaultMicrovm']?.BOOL ?? defaults.engineDefaultMicrovm,
                speakerThresholdOverridden: (item['speakerThreshold']?.S || '').trim() !== '',
            }
            : defaults;
        cached = { config, at: Date.now() };
        return config;
    } catch (error) {
        server.log.warn(
            `[ASR]: could not read the ASR config table; using deployment defaults: ${normalizeErrorForLogging(error)}`
        );
        // Cache the fallback too, so a broken table does not add a DynamoDB timeout
        // to the start of every meeting.
        cached = { config: defaults, at: Date.now() };
        return defaults;
    }
};
