/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Tests for asking which enrolled voices a meeting may label by name.
 *
 * The invariant worth pinning is that this never costs a meeting its
 * transcript. Every way the lookup can go wrong — enrollment off, the function
 * erroring, a malformed answer — has to produce a meeting with voice ids, not a
 * meeting that failed to start. The second invariant is that a vector reaches
 * the engine exactly as stored: it IS the voiceprint, so anything that alters
 * it changes who gets recognised.
 */
import { afterEach, beforeEach, describe, it } from 'node:test';
import assert from 'node:assert';
import { LambdaClient } from '@aws-sdk/client-lambda';

import { fetchEnrolledVoices, voiceEnrollmentEnabled } from './voice-enrollment';

const ARN = 'arn:aws:lambda:us-east-1:123456789012:function:seeds';

const logger = {
    log: {
        info: (): void => {},
        warn: (): void => {},
        error: (): void => {},
        debug: (): void => {},
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
} as any;

interface SentCall {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    command: any;
    options?: { requestTimeout?: number };
}

let sent: SentCall[] = [];
const originalSend = LambdaClient.prototype.send;

/** Same approach as asr-microvm.test.ts: stub the SDK's send on the prototype. */
const respondWith = (responder: () => unknown): void => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (LambdaClient.prototype as any).send = (
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        command: any,
        options?: { requestTimeout?: number }
    ) => {
        sent.push({ command, options });
        return Promise.resolve(responder());
    };
};

const payloadOf = (body: unknown): Uint8Array =>
    new Uint8Array(Buffer.from(JSON.stringify(body)));

describe('fetchEnrolledVoices', () => {
    beforeEach(() => {
        sent = [];
        process.env['VOICE_ENROLLMENT_SEEDS_FUNCTION_ARN'] = ARN;
    });

    afterEach(() => {
        LambdaClient.prototype.send = originalSend;
        delete process.env['VOICE_ENROLLMENT_SEEDS_FUNCTION_ARN'];
    });

    it('asks nothing at all when voice enrollment is off', async () => {
        delete process.env['VOICE_ENROLLMENT_SEEDS_FUNCTION_ARN'];
        assert.equal(voiceEnrollmentEnabled(), false);
        respondWith(() => {
            throw new Error('must not be called');
        });
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
        assert.equal(sent.length, 0);
    });

    it('returns the enrolled voices the function reports', async () => {
        respondWith(() => ({
            Payload: payloadOf({
                EnrolledVoices: [{ label: 'Sarah Lee', vector: [0.1, 0.2, 0.3] }],
            }),
        }));
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), [
            { label: 'Sarah Lee', vector: [0.1, 0.2, 0.3] },
        ]);
    });

    it('passes the vector through unchanged', async () => {
        const vector = [0.1234567890123, -0.9876543210987, 0];
        respondWith(() => ({
            Payload: payloadOf({ EnrolledVoices: [{ label: 'Sarah Lee', vector }] }),
        }));
        const [voice] = await fetchEnrolledVoices('call-1', logger);
        assert.deepEqual(voice?.vector, vector);
    });

    it('sends the call id so a lookup can be traced to its meeting', async () => {
        respondWith(() => ({ Payload: payloadOf({ EnrolledVoices: [] }) }));
        await fetchEnrolledVoices('call-xyz', logger);
        assert.deepEqual(
            JSON.parse(Buffer.from(sent[0]?.command.input.Payload as Uint8Array).toString('utf8')),
            { CallId: 'call-xyz' }
        );
    });

    it('bounds how long a meeting waits for the answer', async () => {
        respondWith(() => ({ Payload: payloadOf({ EnrolledVoices: [] }) }));
        await fetchEnrolledVoices('call-1', logger);
        const timeout = sent[0]?.options?.requestTimeout;
        assert.ok(timeout && timeout > 0, 'a request timeout must be set');
    });

    it('yields no names rather than throwing when the function errors', async () => {
        respondWith(() => ({
            FunctionError: 'Unhandled',
            Payload: payloadOf({ errorMessage: 'boom' }),
        }));
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
    });

    it('yields no names rather than throwing when the invoke rejects', async () => {
        respondWith(() => {
            throw new Error('access denied');
        });
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
    });

    it('yields no names when the answer is not JSON', async () => {
        respondWith(() => ({ Payload: new Uint8Array(Buffer.from('not json at all')) }));
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
    });

    it('yields no names when the answer has no voices in it', async () => {
        respondWith(() => ({ Payload: payloadOf({ Unexpected: true }) }));
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
    });

    it('yields no names when there is no payload at all', async () => {
        respondWith(() => ({}));
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
    });

    it('drops a malformed row and keeps the rest', async () => {
        respondWith(() => ({
            Payload: payloadOf({
                EnrolledVoices: [
                    { label: 'Sarah Lee', vector: [0.1, 0.2] },
                    { label: 42, vector: [0.3, 0.4] },
                    { label: 'Tom Okafor', vector: 'not a vector' },
                    { label: 'Priya Raman', vector: [0.5, 0.6] },
                ],
            }),
        }));
        const voices = await fetchEnrolledVoices('call-1', logger);
        assert.deepEqual(
            voices.map((voice) => voice.label),
            ['Sarah Lee', 'Priya Raman']
        );
    });

    it('drops a vector with a non-numeric component rather than shortening it', async () => {
        // Keeping the numeric part would leave a vector that still compares
        // against the enrolled centroids and matches the wrong person.
        respondWith(() => ({
            Payload: payloadOf({
                EnrolledVoices: [{ label: 'Sarah Lee', vector: [0.1, null, 0.3] }],
            }),
        }));
        assert.deepEqual(await fetchEnrolledVoices('call-1', logger), []);
    });

    it('makes a fresh call per meeting, so a withdrawal takes effect at once', async () => {
        respondWith(() => ({ Payload: payloadOf({ EnrolledVoices: [] }) }));
        await fetchEnrolledVoices('call-1', logger);
        await fetchEnrolledVoices('call-2', logger);
        assert.equal(sent.length, 2);
    });
});
