/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the transcriber's metering records.
 *
 * Pinned here: the billed-seconds rule (whole seconds, a 15-second minimum per
 * request, and a stereo stream counted once rather than per channel), the item
 * shape the cost estimate reads, and that a failed write never reaches the
 * transcription that incurred it.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, test } from 'node:test';
import {
    buildMeteringItem,
    meterMicrovm,
    meterTranscribeRequest,
    pcmSeconds,
    recordMetering,
    setMeteringClient,
    transcribeBilledSeconds,
} from './metering';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- the item is asserted structurally
type Sent = { input: { TableName: string; Item: Record<string, any> } };

let sent: Sent[] = [];
let fail = false;

beforeEach(() => {
    sent = [];
    fail = false;
    process.env['METERING_TABLE_NAME'] = 'calls';
    process.env['METERING_EXPIRATION_DAYS'] = '90';
    setMeteringClient({
        send: (async (command: Sent) => {
            if (fail) {
                throw new Error('throttled');
            }
            sent.push(command);
            return {};
        }) as never,
    });
});

afterEach(() => {
    setMeteringClient(undefined);
    delete process.env['METERING_TABLE_NAME'];
    delete process.env['METERING_EXPIRATION_DAYS'];
});

test('a request is billed in whole seconds with a 15-second minimum', () => {
    assert.equal(transcribeBilledSeconds(0), 15);
    assert.equal(transcribeBilledSeconds(14.2), 15);
    assert.equal(transcribeBilledSeconds(15.01), 16);
    assert.equal(transcribeBilledSeconds(3600), 3600);
});

test('a stereo stream is measured at its duration, not once per channel', () => {
    // One second of 16 kHz, 16-bit, two-channel PCM.
    assert.equal(pcmSeconds(16000 * 2 * 2, 16000, 2), 1);
    assert.equal(pcmSeconds(100, 0, 2), 0);
});

test('the item has the shape the cost estimate reads', () => {
    const now = new Date('2026-10-28T12:00:00.000Z');
    const item = buildMeteringItem(
        'Meeting-1',
        'Transcription',
        'transcribe/streaming',
        { seconds: 1200, requests: 1 },
        { source: 'websocket-transcriber', recordId: 'r', now, expirationDays: 0.5, details: { channels: 2 } }
    );
    assert.deepEqual(item, {
        PK: { S: 'mtr#Meeting-1' },
        SK: { S: 'Transcription#2026-10-28T12:00:00.000Z#r' },
        CallId: { S: 'Meeting-1' },
        Context: { S: 'Transcription' },
        Service: { S: 'transcribe/streaming' },
        Usage: { M: { seconds: { N: '1200' }, requests: { N: '1' } } },
        Source: { S: 'websocket-transcriber' },
        RecordedAt: { S: '2026-10-28T12:00:00.000Z' },
        Details: { M: { channels: { S: '2' } } },
        ExpiresAfter: { N: String(Math.floor(now.getTime() / 1000) + 43200) },
    });
});

test('numbers are written in plain decimal, as the Python writer writes them', () => {
    const item = buildMeteringItem('c', 'x', 's', { a: 0.0000125, b: 33.5 }, { source: 't' });
    assert.deepEqual(item?.['Usage'], { M: { a: { N: '0.000013' }, b: { N: '33.5' } } });
});

test('a record that says nothing is not built', () => {
    assert.equal(buildMeteringItem('', 'x', 's', { a: 1 }, { source: 't' }), undefined);
    assert.equal(buildMeteringItem('c', 'x', 's', { a: Number.NaN }, { source: 't' }), undefined);
});

test('a request with redaction records the add-on as its own line', async () => {
    await meterTranscribeRequest('c', 42.3, {
        callAnalytics: false,
        redaction: true,
        customLanguageModel: false,
        channels: 2,
    });
    const services = sent.map((s) => s.input.Item['Service'].S).sort();
    assert.deepEqual(services, ['transcribe/streaming', 'transcribe/streaming-pii-redaction']);
    for (const s of sent) {
        assert.deepEqual(s.input.Item['Usage'].M['seconds'], { N: '43' });
    }
});

test('a Call Analytics request is metered at the Call Analytics price', async () => {
    await meterTranscribeRequest('c', 5, {
        callAnalytics: true,
        redaction: false,
        customLanguageModel: true,
        channels: 2,
    });
    const services = sent.map((s) => s.input.Item['Service'].S).sort();
    assert.deepEqual(services, [
        'transcribe/streaming-call-analytics',
        'transcribe/streaming-custom-language-model',
    ]);
});

test('a MicroVM is metered at its configured vCPU and memory for its lifetime', async () => {
    assert.equal(await meterMicrovm('c', 600, 8192, {}), true);
    const usage = sent[0].input.Item['Usage'].M;
    assert.deepEqual(usage['vcpuSeconds'], { N: '2400' });
    assert.deepEqual(usage['gbSeconds'], { N: '4800' });
    assert.equal(sent[0].input.Item['Service'].S, 'lambda-microvm/arm64');
});

test('a failed write resolves to false rather than rejecting', async () => {
    fail = true;
    const warnings: string[] = [];
    const stored = await recordMetering('c', 'x', 's', { a: 1 }, undefined, {
        warn: (m) => warnings.push(m),
    });
    assert.equal(stored, false);
    assert.equal(warnings.length, 1);
});

test('nothing is written when no table is configured', async () => {
    delete process.env['METERING_TABLE_NAME'];
    assert.equal(await recordMetering('c', 'x', 's', { a: 1 }), false);
    assert.equal(sent.length, 0);
});
