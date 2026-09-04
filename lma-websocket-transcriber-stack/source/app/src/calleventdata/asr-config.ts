/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Runtime switches for the MicroVM ASR engine.
 *
 * Three booleans, edited from the ASR Config admin page and read here at meeting
 * start so a change needs no stack update. There are deliberately no
 * tuning fields: the diarization operating point (similarity threshold, minimum
 * utterance length) is measured for the model bundle and baked into the ASR image,
 * so no deployment has to know a number for it.
 */
import { FastifyInstance } from 'fastify';
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';

import { normalizeErrorForLogging } from '../utils/common';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const ASR_CONFIG_TABLE_NAME = process.env['ASR_CONFIG_TABLE_NAME'] || '';
const ASR_CONFIG_ID = 'CustomAsrConfig';
// Re-read at most this often. A meeting picks up a change on its next start;
// caching keeps a burst of concurrent meetings from each doing their own GetItem.
const ASR_CONFIG_TTL_MS = parseInt(process.env['ASR_CONFIG_TTL_MS'] || '30000', 10);

const dynamoClient = new DynamoDBClient({ region: AWS_REGION });

export interface AsrRuntimeConfig {
    /**
     * Streaming meetings (Stream Audio, the Chrome extension, the Desktop Capture
     * apps) use the on-demand
     * engine. Off means Amazon Transcribe. A client that names an engine in its
     * START frame (the desktop apps' --asr-engine) still wins.
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

let cached: { config: AsrRuntimeConfig; at: number } | undefined;

/** Exposed for tests: forget the cached record. */
export const resetAsrConfigCache = (): void => {
    cached = undefined;
};

/**
 * Effective config for a meeting: the DynamoDB record over the env defaults.
 *
 * Never throws. A failed read logs and returns the defaults, because losing the
 * ability to transcribe over a config lookup would be a far worse failure than
 * running on Amazon Transcribe.
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
                streamingEngineMicrovm:
                    item['streamingEngineMicrovm']?.BOOL ?? defaults.streamingEngineMicrovm,
                virtualParticipantEngineMicrovm:
                    item['virtualParticipantEngineMicrovm']?.BOOL ?? defaults.virtualParticipantEngineMicrovm,
                diarizeVirtualParticipant:
                    item['diarizeVirtualParticipant']?.BOOL ?? defaults.diarizeVirtualParticipant,
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
