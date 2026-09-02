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

test('a deliberately named threshold is distinguished from the bundle default', async () => {
    // For a pairing nobody has calibrated, this flag is the only evidence the
    // operating point was ever checked, so it decides whether labels are produced.
    // Either an admin typing one or an operator setting the stack parameter counts.
    resetAsrConfigCache();
    let restore = stubDynamo(async () => ({ Item: { speakerThreshold: { S: '0.35' } } }));
    assert.equal((await getAsrRuntimeConfig(fakeServer)).speakerThresholdOverridden, true);
    restore();

    // The stack parameter alone (set at the top of this file) is also deliberate.
    resetAsrConfigCache();
    restore = stubDynamo(async () => ({ Item: { speakerThreshold: { S: '' } } }));
    assert.equal((await getAsrRuntimeConfig(fakeServer)).speakerThresholdOverridden, true);
    restore();

    // With neither, the bundle's baked value is in force and nobody has named one.
    const stackValue = process.env['ASR_SPEAKER_THRESHOLD'];
    delete process.env['ASR_SPEAKER_THRESHOLD'];
    resetAsrConfigCache();
    restore = stubDynamo(async () => ({}));
    const config = await getAsrRuntimeConfig(fakeServer);
    restore();
    process.env['ASR_SPEAKER_THRESHOLD'] = stackValue;

    assert.equal(config.speakerThresholdOverridden, false);
    // Undefined, not a constant: the engine then uses the value calibrated for the
    // deployed bundle rather than a number that was right for one other pairing.
    assert.equal(config.speakerThreshold, undefined);
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
