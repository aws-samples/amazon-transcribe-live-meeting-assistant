/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the Virtual Participant's metering records: the compute arithmetic
 * per launch type, the Nova Sonic token accumulator, and the item shape the
 * meeting cost estimate reads.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, test } from 'node:test';
import {
    NovaSonicUsage,
    anthropicUsage,
    buildMeteringItem,
    priceArchitecture,
    recordMetering,
    resolveComputeSize,
    setMeteringClient,
    transcribeBilledSeconds,
    virtualParticipantUsage,
} from './metering.js';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- items are asserted structurally
type Sent = { input: { Item: Record<string, any> } };
let sent: Sent[] = [];

beforeEach(() => {
    sent = [];
    process.env.METERING_TABLE_NAME = 'calls';
    setMeteringClient({
        send: (async (command: Sent) => {
            sent.push(command);
            return {};
        }) as never,
    });
});

afterEach(() => {
    setMeteringClient(undefined);
    delete process.env.METERING_TABLE_NAME;
    delete process.env.VP_MICROVM_MEMORY_MIB;
    delete process.env.ECS_CONTAINER_METADATA_URI_V4;
});

const SIZE = { vcpus: 2, memoryMiB: 4096, architecture: 'x86_64' as const, sizeSource: 'test' };

test('a Fargate task is billed for its vCPU and memory, with a one-minute minimum', () => {
    const long = virtualParticipantUsage('FARGATE', 1800, SIZE);
    assert.equal(long.service, 'fargate/x86_64');
    assert.deepEqual(long.usage, { vcpuSeconds: 3600, gbSeconds: 7200, runtimeSeconds: 1800 });
    const short = virtualParticipantUsage('FARGATE', 20, SIZE);
    assert.equal(short.usage.vcpuSeconds, 120);
    assert.equal(short.usage.runtimeSeconds, 20);
});

test('an arm64 Fargate task is priced as arm64', () => {
    assert.equal(virtualParticipantUsage('FARGATE', 60, { ...SIZE, architecture: 'arm64' }).service, 'fargate/arm64');
    assert.equal(priceArchitecture('x64'), 'x86_64');
    assert.equal(priceArchitecture('arm64'), 'arm64');
});

test('a MicroVM is billed for exactly its runtime, at its size', () => {
    const run = virtualParticipantUsage('MICROVM', 10, { ...SIZE, architecture: 'arm64' });
    assert.equal(run.service, 'lambda-microvm/arm64');
    assert.deepEqual(run.usage, { vcpuSeconds: 20, gbSeconds: 40, runtimeSeconds: 10 });
});

test('an EC2 host is not attributed to the meeting; only its runtime is recorded', () => {
    assert.deepEqual(virtualParticipantUsage('EC2', 900, SIZE), {
        service: 'ec2/virtual-participant-host',
        usage: { runtimeSeconds: 900 },
    });
});

test('a MicroVM is sized from its image memory at 2 GiB per vCPU', async () => {
    assert.deepEqual(await resolveComputeSize('MICROVM'), {
        vcpus: 2,
        memoryMiB: 4096,
        architecture: 'arm64',
        sizeSource: 'image default',
    });
    process.env.VP_MICROVM_MEMORY_MIB = '8192';
    assert.equal((await resolveComputeSize('MICROVM')).vcpus, 4);
});

test('a Fargate task is sized from the ECS task metadata when it is there', async () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = 'http://169.254.170.2/v4/x';
    const fetcher = (async (url: string) => {
        assert.equal(url, 'http://169.254.170.2/v4/x/task');
        return { json: async () => ({ Limits: { CPU: 4, Memory: 8192 } }) };
    }) as unknown as typeof fetch;
    const size = await resolveComputeSize('FARGATE', fetcher);
    assert.equal(size.vcpus, 4);
    assert.equal(size.memoryMiB, 8192);
    assert.equal(size.sizeSource, 'ECS task metadata');
});

test('Nova Sonic usage adds each delta once and is written as one record per flush', async () => {
    const usage = new NovaSonicUsage('amazon.nova-2-sonic-v1:0');
    usage.noteSession();
    const event = (inSpeech: number, outSpeech: number) => ({
        details: {
            delta: { input: { speechTokens: inSpeech, textTokens: 10 }, output: { speechTokens: outSpeech, textTokens: 0 } },
            total: { input: { speechTokens: 99999, textTokens: 99999 }, output: { speechTokens: 99999, textTokens: 99999 } },
        },
    });
    usage.add(event(100, 40));
    usage.add(event(50, 0));
    usage.add({});
    assert.deepEqual(usage.snapshot(), {
        speechInputTokens: 150,
        textInputTokens: 20,
        speechOutputTokens: 40,
        sessions: 1,
    });
    assert.equal(await usage.flush('Meeting-1'), true);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].input.Item.Context.S, 'VoiceAssistant');
    assert.equal(sent[0].input.Item.Service.S, 'bedrock/amazon.nova-2-sonic-v1:0');
    assert.equal(await usage.flush('Meeting-1'), false, 'nothing new to write');
});

test('Nova Sonic invoked in another region records that region', async () => {
    const usage = new NovaSonicUsage('amazon.nova-2-sonic-v1:0', 'us-west-2');
    usage.add({ details: { delta: { output: { speechTokens: 5 } } } });
    await usage.flush('Meeting-1');
    assert.deepEqual(sent[0].input.Item.Region, { S: 'us-west-2' });
});

test('an Anthropic-native usage block maps onto the price table units', () => {
    assert.deepEqual(
        anthropicUsage({ input_tokens: 1500, output_tokens: 120, cache_read_input_tokens: 0 }),
        { requests: 1, inputTokens: 1500, outputTokens: 120 },
    );
    assert.deepEqual(anthropicUsage(undefined), { requests: 1 });
});

test('the item has the shared shape', () => {
    const now = new Date('2026-10-28T12:00:00.000Z');
    const item = buildMeteringItem('m', 'VirtualParticipant', 'fargate/x86_64', { vcpuSeconds: 1 }, {
        source: 'virtual-participant',
        recordId: 'r',
        now,
    });
    assert.deepEqual(item?.PK, { S: 'mtr#m' });
    assert.deepEqual(item?.SK, { S: 'VirtualParticipant#2026-10-28T12:00:00.000Z#r' });
    assert.equal(item?.ExpiresAfter, undefined, 'no TTL without a retention setting');
    assert.equal(transcribeBilledSeconds(3.2), 15);
});

test('nothing is written without a meeting', async () => {
    assert.equal(await recordMetering('', 'x', 's', { a: 1 }), false);
    assert.equal(sent.length, 0);
});
