/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
process.env['ASR_CONFIG_TABLE_NAME'] = 'asr-config-test';
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';
delete process.env['ASR_ENGINE_DEFAULT'];

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';

import { DynamoDBClient } from '@aws-sdk/client-dynamodb';

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

/**
 * Stub send() on the client the module already constructed at import time, which
 * is why this patches the prototype rather than injecting a client.
 */
const stubDynamo = (behaviour: () => Promise<unknown>): (() => void) => {
    const prototype = DynamoDBClient.prototype as unknown as { send: unknown };
    const original = prototype.send;
    prototype.send = behaviour;
    return () => {
        prototype.send = original;
    };
};

test('the record decides the deployment default engine', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({
        Item: { AsrConfigId: { S: 'CustomAsrConfig' }, engineDefaultMicrovm: { BOOL: true } },
    }));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.engineDefaultMicrovm, true);
    // A field the record does not carry keeps its default rather than becoming undefined.
    assert.equal(config.diarizeVirtualParticipant, false);
});

test('both switches default off when there is no record, so a fresh deployment stays on Transcribe', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({}));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(config, { engineDefaultMicrovm: false, diarizeVirtualParticipant: false });
});

test('retired tuning fields in an old record are ignored', async () => {
    // A table written by a previous release may still hold a threshold. It must
    // not leak into the config: the operating point lives in the image now.
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({
        Item: {
            speakerThreshold: { S: '0.35' },
            minSegmentMs: { S: '2500' },
            liveTurnCut: { BOOL: false },
            diarizeVirtualParticipant: { BOOL: true },
        },
    }));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.deepEqual(Object.keys(config).sort(), ['diarizeVirtualParticipant', 'engineDefaultMicrovm']);
    assert.equal(config.diarizeVirtualParticipant, true);
});

test('a failed read degrades to the defaults instead of throwing', async () => {
    resetAsrConfigCache();
    warnings.length = 0;
    const restore = stubDynamo(async () => {
        throw new Error('AccessDeniedException');
    });
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    // Losing transcription over a config lookup would be far worse than running
    // on Amazon Transcribe.
    assert.equal(config.engineDefaultMicrovm, false);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /using deployment defaults/);
});

test('the record is cached, so concurrent meetings do not each read it', async () => {
    resetAsrConfigCache();
    let reads = 0;
    const restore = stubDynamo(async () => {
        reads += 1;
        return { Item: { engineDefaultMicrovm: { BOOL: true } } };
    });
    await getAsrRuntimeConfig(fakeServer);
    await getAsrRuntimeConfig(fakeServer);
    await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(reads, 1);
});
