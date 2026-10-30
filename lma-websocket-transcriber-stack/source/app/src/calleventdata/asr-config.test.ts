/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where a streaming meeting's ASR switches come from, and how long they are reused.
 *
 * The switches live in the shared configuration table as the `asr_engine` section.
 * What is asserted here is the part that is silent when wrong:
 *
 * - The configuration table decides, and the previous table is read only when the
 *   configuration table cannot be read or holds no shipped switches.
 * - A resolved read is reused for 30 seconds -- the `short` tier -- so concurrent
 *   meetings do not each read, and a fallback answer is reused for as long.
 * - A failed read never stops a meeting: it degrades to Amazon Transcribe.
 *
 * DynamoDB is stubbed at the client prototype, which answers both the accessor's
 * reads (keyed `Configuration`) and the previous table's (keyed `AsrConfigId`).
 */
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';
delete process.env['ASR_ENGINE_DEFAULT'];

import assert from 'node:assert/strict';
import test from 'node:test';
import { gzipSync } from 'node:zlib';
import { FastifyInstance } from 'fastify';

import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';

import { getAsrRuntimeConfig, resetAsrConfigCache } from './asr-config';

const warnings: string[] = [];
const fakeServer = {
    log: {
        info: () => undefined,
        warn: (message: string) => warnings.push(message),
        error: () => undefined,
        debug: () => undefined,
    },
} as unknown as FastifyInstance;

type Item = Record<string, AttributeValue>;

const record = (key: string, body: unknown): Item => ({
    Configuration: { S: key },
    Version: { N: '1' },
    Body: { B: gzipSync(Buffer.from(JSON.stringify(body), 'utf-8')) },
});

const SHIPPED = {
    streamingEngineMicrovm: false,
    virtualParticipantEngineMicrovm: false,
    diarizeVirtualParticipant: false,
};

/** The configuration table, with the operator's profile holding `custom`. */
/** `shipped: null` stands for a baseline holding no ASR switches at all. */
const configurationTable = (custom: Record<string, unknown> | undefined, shipped: unknown = SHIPPED) => {
    const items = new Map<string, Item>([
        ['Config#default', record('Config#default', shipped === null ? {} : { asr_engine: shipped })],
        ['Config#__active', { Configuration: { S: 'Config#__active' }, ActiveProfile: { S: 'custom' } }],
    ]);
    items.set('Config#custom', record('Config#custom', custom === undefined ? {} : { asr_engine: custom }));
    return items;
};

interface Reads {
    configuration: number;
    legacy: number;
}

/**
 * Stub send() on every DynamoDB client, which is why this patches the prototype
 * rather than injecting one: the module builds its client at import time.
 */
const stubDynamo = (
    configuration: Map<string, Item> | 'fail' | undefined,
    legacy: Item | 'fail' | undefined
): { reads: Reads; restore: () => void } => {
    const reads: Reads = { configuration: 0, legacy: 0 };
    const prototype = DynamoDBClient.prototype as unknown as { send: unknown };
    const original = prototype.send;
    prototype.send = async (command: { input: { Key: Record<string, { S: string }> } }) => {
        const key = command.input.Key;
        if (key['Configuration']) {
            reads.configuration += 1;
            if (configuration === 'fail' || configuration === undefined) {
                throw new Error('AccessDeniedException');
            }
            const item = configuration.get(key['Configuration'].S);
            return item ? { Item: item } : {};
        }
        reads.legacy += 1;
        if (legacy === 'fail') {
            throw new Error('AccessDeniedException');
        }
        return legacy ? { Item: legacy } : {};
    };
    return {
        reads,
        restore: () => {
            prototype.send = original;
        },
    };
};

const withTables = (configurationTable: string, legacyTable: string): void => {
    process.env['CONFIGURATION_TABLE_NAME'] = configurationTable;
    process.env['ASR_CONFIG_TABLE_NAME'] = legacyTable;
    resetAsrConfigCache();
    warnings.length = 0;
};

// ── the configuration table ─────────────────────────────────────────────────

test('the configuration table decides which engine streaming meetings use', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { reads, restore } = stubDynamo(configurationTable({ streamingEngineMicrovm: true }), undefined);
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.streamingEngineMicrovm, true);
    // Switches the profile does not set come from the shipped baseline.
    assert.equal(config.virtualParticipantEngineMicrovm, false);
    assert.equal(config.diarizeVirtualParticipant, false);
    assert.equal(reads.legacy, 0, 'the previous table was read');
});

test('a fresh deployment stays on Amazon Transcribe', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { restore } = stubDynamo(configurationTable({}), undefined);
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(config, SHIPPED);
});

test('only the three switches are returned, whatever else the section holds', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { restore } = stubDynamo(
        configurationTable({ diarizeVirtualParticipant: true, speakerThreshold: '0.35' }),
        undefined
    );
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(Object.keys(config).sort(), [
        'diarizeVirtualParticipant',
        'streamingEngineMicrovm',
        'virtualParticipantEngineMicrovm',
    ]);
    assert.equal(config.diarizeVirtualParticipant, true);
});

test('a resolved read is reused, so concurrent meetings do not each read it', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { reads, restore } = stubDynamo(configurationTable({ streamingEngineMicrovm: true }), undefined);
    await getAsrRuntimeConfig(fakeServer);
    const afterFirst = reads.configuration;
    await getAsrRuntimeConfig(fakeServer);
    await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.ok(afterFirst > 0);
    assert.equal(reads.configuration, afterFirst, 'a cached document was read again within the tier');
});

// ── the fallback to the previous table ──────────────────────────────────────

test('a configuration table without shipped switches falls back to the previous table', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { reads, restore } = stubDynamo(configurationTable({ streamingEngineMicrovm: false }, null), {
        AsrConfigId: { S: 'CustomAsrConfig' },
        streamingEngineMicrovm: { BOOL: true },
    });
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(reads.legacy, 1);
    assert.equal(config.streamingEngineMicrovm, true, 'the profile layer was used with nothing shipped beneath it');
});

test('a configuration table that cannot be read falls back to the previous table', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { restore } = stubDynamo('fail', { diarizeVirtualParticipant: { BOOL: true } });
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.diarizeVirtualParticipant, true);
    assert.match(warnings[0] ?? '', /trying the previous ASR config table/);
});

test('a fallback answer is reused too, so a broken table is not retried at every meeting start', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { reads, restore } = stubDynamo('fail', 'fail');
    await getAsrRuntimeConfig(fakeServer);
    const afterFirst = { ...reads };
    await getAsrRuntimeConfig(fakeServer);
    await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(reads, afterFirst);
});

test('with only the previous table named, it is read as before', async () => {
    withTables('', 'asr-config-test');
    const { reads, restore } = stubDynamo(undefined, {
        AsrConfigId: { S: 'CustomAsrConfig' },
        virtualParticipantEngineMicrovm: { BOOL: true },
    });
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(reads.configuration, 0);
    assert.equal(config.virtualParticipantEngineMicrovm, true);
    assert.equal(config.streamingEngineMicrovm, false);
});

test('retired tuning fields in an old record are ignored', async () => {
    // A table written by a previous release may still hold a threshold. It must
    // not leak into the config: the operating point lives in the image now.
    withTables('', 'asr-config-test');
    const { restore } = stubDynamo(undefined, {
        speakerThreshold: { S: '0.35' },
        minSegmentMs: { S: '2500' },
        liveTurnCut: { BOOL: false },
        diarizeVirtualParticipant: { BOOL: true },
    });
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(Object.keys(config).sort(), [
        'diarizeVirtualParticipant',
        'streamingEngineMicrovm',
        'virtualParticipantEngineMicrovm',
    ]);
    assert.equal(config.diarizeVirtualParticipant, true);
});

test('a failed read everywhere degrades to the defaults instead of throwing', async () => {
    withTables('lma-configuration', 'asr-config-test');
    const { restore } = stubDynamo('fail', 'fail');
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    // Losing transcription over a config lookup would be far worse than running
    // on Amazon Transcribe.
    assert.equal(config.streamingEngineMicrovm, false);
    assert.match(warnings[warnings.length - 1] ?? '', /using deployment defaults/);
});

test('no table named at all gives the defaults without a read', async () => {
    withTables('', '');
    const { reads, restore } = stubDynamo(undefined, undefined);
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(config, SHIPPED);
    assert.deepEqual(reads, { configuration: 0, legacy: 0 });
});
