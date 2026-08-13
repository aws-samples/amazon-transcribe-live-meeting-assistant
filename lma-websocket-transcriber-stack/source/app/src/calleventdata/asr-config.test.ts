/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
process.env['ASR_CONFIG_TABLE_NAME'] = 'asr-config-test';
process.env['ASR_SPEAKER_THRESHOLD'] = '0.2';
process.env['ASR_MAX_SPEAKERS'] = '0';
process.env['ASR_ENDPOINTING_MS'] = '1200';
process.env['ASR_MIN_SEGMENT_MS'] = '2500';
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';

import { DynamoDBClient } from '@aws-sdk/client-dynamodb';

import { getAsrRuntimeConfig, isSpeakerModelMeasured, resetAsrConfigCache } from './asr-config';

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

test('a DynamoDB override wins over the deployment default', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({
        Item: { AsrConfigId: { S: 'CustomAsrConfig' }, speakerThreshold: { S: '0.35' } },
    }));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.speakerThreshold, 0.35);
    // Everything not overridden still comes from the stack parameters.
    assert.equal(config.endpointingMs, 1200);
    assert.equal(config.minSegmentMs, 2500);
});

test('a blank override falls back to the deployment default, not zero', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({
        Item: { speakerThreshold: { S: '' }, maxSpeakers: { S: '   ' } },
    }));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.speakerThreshold, 0.2);
    assert.equal(config.maxSpeakers, 0);
});

test('a non-numeric override is ignored rather than becoming NaN', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({ Item: { speakerThreshold: { S: 'high' } } }));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.speakerThreshold, 0.2);
});

test('booleans come through and default when absent', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({
        Item: { requireCorroboration: { BOOL: true }, engineDefaultMicrovm: { BOOL: true } },
    }));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.requireCorroboration, true);
    assert.equal(config.engineDefaultMicrovm, true);
    assert.equal(config.diarizeByDefault, true);
});

test('no record at all means every value comes from the stack parameters', async () => {
    resetAsrConfigCache();
    const restore = stubDynamo(async () => ({}));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(config.speakerThreshold, 0.2);
    assert.equal(config.engineDefaultMicrovm, false);
});

test('a failed read degrades to the deployment defaults instead of throwing', async () => {
    resetAsrConfigCache();
    warnings.length = 0;
    const restore = stubDynamo(async () => {
        throw new Error('AccessDeniedException');
    });
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();

    // Losing transcription over a config lookup would be far worse than running
    // with the deployment's own parameters.
    assert.equal(config.speakerThreshold, 0.2);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /using deployment defaults/);
});

test('an admin-set threshold is distinguished from the stack default', async () => {
    // For a speaker model nobody has measured, this flag is the only evidence the
    // operating point was ever checked, so it decides whether labels are produced.
    resetAsrConfigCache();
    let restore = stubDynamo(async () => ({ Item: { speakerThreshold: { S: '0.35' } } }));
    assert.equal((await getAsrRuntimeConfig(fakeServer)).speakerThresholdOverridden, true);
    restore();

    resetAsrConfigCache();
    restore = stubDynamo(async () => ({ Item: { speakerThreshold: { S: '' } } }));
    assert.equal((await getAsrRuntimeConfig(fakeServer)).speakerThresholdOverridden, false);
    restore();

    resetAsrConfigCache();
    restore = stubDynamo(async () => ({}));
    assert.equal((await getAsrRuntimeConfig(fakeServer)).speakerThresholdOverridden, false);
    restore();
});

test('an unmeasured speaker model is reported as such', () => {
    delete process.env['ASR_SPEAKER_MODEL_MEASURED'];
    assert.equal(isSpeakerModelMeasured(), true, 'unset must not degrade an existing deployment');

    process.env['ASR_SPEAKER_MODEL_MEASURED'] = 'false';
    assert.equal(isSpeakerModelMeasured(), false);
    delete process.env['ASR_SPEAKER_MODEL_MEASURED'];
});

test('the record is cached, so concurrent meetings do not each read it', async () => {
    resetAsrConfigCache();
    let reads = 0;
    const restore = stubDynamo(async () => {
        reads += 1;
        return { Item: { speakerThreshold: { S: '0.3' } } };
    });
    await getAsrRuntimeConfig(fakeServer);
    await getAsrRuntimeConfig(fakeServer);
    await getAsrRuntimeConfig(fakeServer);
    restore();

    assert.equal(reads, 1);
});
