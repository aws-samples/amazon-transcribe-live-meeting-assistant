/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Runtime switches for the MicroVM ASR engine.
 *
 * Three booleans, edited on the Transcription Engine page and read at meeting start. No
 * tuning fields: the operating point is baked into the ASR image per bundle.
 *
 * Where they are read from
 * ------------------------
 * The shared configuration table (`CONFIGURATION_TABLE_NAME`), as the `asr_engine`
 * section, on the `short` tier: a resolved read is reused for 30 seconds, so a burst of
 * meetings starting together does not each issue its own reads, and a change reaches
 * every meeting that starts more than 30 seconds after it was saved.
 *
 * The table the switches were stored in before (`ASR_CONFIG_TABLE_NAME`) is read only
 * when the configuration table cannot be read or holds no shipped ASR switches -- a
 * stack rolled back across the move, or a table whose seeder has not run. Its record
 * holds the switches as they were saved before the move.
 */
import { FastifyInstance } from 'fastify';
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';

import { ASR_ENGINE, ConfigurationStore, TIER_TTL_MS } from '../config/configuration-store';
import { normalizeErrorForLogging } from '../utils/common';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const ASR_CONFIG_ID = 'CustomAsrConfig';
// The fallback paths below keep what they read for as long as the section's own tier
// would, so the two routes to the same switches agree on how stale they may be.
const FALLBACK_TTL_MS = TIER_TTL_MS[ASR_ENGINE.cacheTier];

const dynamoClient = new DynamoDBClient({ region: AWS_REGION });

export interface AsrRuntimeConfig {
    /**
     * Streaming meetings (Stream Audio, Chrome extension, Desktop Capture apps) use
     * the on-demand engine; the desktop apps' --asr-engine still wins.
     */
    streamingEngineMicrovm: boolean;
    /**
     * Read by the Virtual Participant, not here; carried so one type describes
     * the whole record.
     */
    virtualParticipantEngineMicrovm: boolean;
    /** Also the Virtual Participant's: ask the engine for per-voice labels. */
    diarizeVirtualParticipant: boolean;
}

// ASR_ENGINE_DEFAULT is a development escape hatch for a transcriber running with
// no config table (ASR_DIRECT_ENDPOINT against a local engine); deployed stacks do
// not set it, so the table is the only place the default lives.
const envDefaults = (): AsrRuntimeConfig => ({
    streamingEngineMicrovm: (process.env['ASR_ENGINE_DEFAULT'] || 'transcribe').toLowerCase() === 'microvm',
    virtualParticipantEngineMicrovm: false,
    diarizeVirtualParticipant: false,
});

// Read when called rather than at import, so a test can name either table.
const configurationTableName = (): string => process.env['CONFIGURATION_TABLE_NAME'] || '';
const legacyTableName = (): string => process.env['ASR_CONFIG_TABLE_NAME'] || '';

/** One per process: the store's cache lives on the instance. */
let store: ConfigurationStore | undefined;
/** What a fallback path last answered, and when. */
let fallback: { config: AsrRuntimeConfig; at: number } | undefined;

/** Exposed for tests: forget everything read so far. */
export const resetAsrConfigCache = (): void => {
    store = undefined;
    fallback = undefined;
};

const asSwitch = (value: unknown, fallbackValue: boolean): boolean =>
    typeof value === 'boolean' ? value : fallbackValue;

const fromValues = (values: Record<string, unknown>, defaults: AsrRuntimeConfig): AsrRuntimeConfig => ({
    streamingEngineMicrovm: asSwitch(values['streamingEngineMicrovm'], defaults.streamingEngineMicrovm),
    virtualParticipantEngineMicrovm: asSwitch(
        values['virtualParticipantEngineMicrovm'],
        defaults.virtualParticipantEngineMicrovm
    ),
    diarizeVirtualParticipant: asSwitch(values['diarizeVirtualParticipant'], defaults.diarizeVirtualParticipant),
});

/** The switches from the configuration table, or undefined to fall back. */
const fromConfigurationTable = async (
    server: FastifyInstance,
    defaults: AsrRuntimeConfig
): Promise<AsrRuntimeConfig | undefined> => {
    const tableName = configurationTableName();
    if (!tableName) {
        return undefined;
    }
    try {
        store = store ?? new ConfigurationStore({ tableName, client: dynamoClient });
        const section = await store.getSection<Record<string, unknown>>(ASR_ENGINE);
        if (Object.keys(section.baselineValues).length === 0) {
            server.log.warn(
                `[ASR]: the configuration table ${tableName} holds no shipped ASR switches; ` +
                    'reading the previous ASR config table instead'
            );
            return undefined;
        }
        return fromValues(section.values, defaults);
    } catch (error) {
        server.log.warn(
            `[ASR]: could not read the configuration table; trying the previous ASR config table: ${normalizeErrorForLogging(error)}`
        );
        return undefined;
    }
};

/** The switches from the previous table, as they were saved before the move. */
const fromLegacyTable = async (defaults: AsrRuntimeConfig): Promise<AsrRuntimeConfig> => {
    const result = await dynamoClient.send(
        new GetItemCommand({
            TableName: legacyTableName(),
            Key: { AsrConfigId: { S: ASR_CONFIG_ID } },
        })
    );
    const item = result.Item;
    return item
        ? {
            streamingEngineMicrovm:
                item['streamingEngineMicrovm']?.BOOL ?? defaults.streamingEngineMicrovm,
            virtualParticipantEngineMicrovm:
                item['virtualParticipantEngineMicrovm']?.BOOL ?? defaults.virtualParticipantEngineMicrovm,
            diarizeVirtualParticipant:
                item['diarizeVirtualParticipant']?.BOOL ?? defaults.diarizeVirtualParticipant,
        }
        : defaults;
};

/**
 * Effective config for a meeting: the stored switches over the env defaults.
 *
 * Never throws. A failed read logs and returns the defaults, because losing the
 * ability to transcribe over a config lookup would be a far worse failure than
 * running on Amazon Transcribe.
 */
export const getAsrRuntimeConfig = async (
    server: FastifyInstance
): Promise<AsrRuntimeConfig> => {
    const defaults = envDefaults();
    if (!configurationTableName() && !legacyTableName()) {
        return defaults;
    }

    // A fallback answered recently stands for its TTL, so a configuration table that
    // cannot be read does not add a failed read to the start of every meeting.
    if (fallback && Date.now() - fallback.at < FALLBACK_TTL_MS) {
        return fallback.config;
    }
    const fromStore = await fromConfigurationTable(server, defaults);
    if (fromStore) {
        return fromStore;
    }
    if (!legacyTableName()) {
        fallback = { config: defaults, at: Date.now() };
        return defaults;
    }

    try {
        const config = await fromLegacyTable(defaults);
        fallback = { config, at: Date.now() };
        return config;
    } catch (error) {
        server.log.warn(
            `[ASR]: could not read the ASR config table; using deployment defaults: ${normalizeErrorForLogging(error)}`
        );
        // Cache the fallback too, so a broken table does not add a DynamoDB timeout
        // to the start of every meeting.
        fallback = { config: defaults, at: Date.now() };
        return defaults;
    }
};
