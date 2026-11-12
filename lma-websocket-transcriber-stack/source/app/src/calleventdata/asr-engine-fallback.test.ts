/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// A deployment without the ASR MicroVM engine: no launcher and no direct
// endpoint. Read at import time, so set before the module under test loads; a
// file of its own because the other ASR tests configure a launcher.
delete process.env['ASR_LAUNCHER_FUNCTION_ARN'];
delete process.env['ASR_DIRECT_ENDPOINT'];
delete process.env['ASR_ENGINE_DEFAULT'];
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';

import { resolveAsrEngine } from './asr-microvm';
import { AsrRuntimeConfig } from './asr-config';
import { CallMetaData } from './eventtypes';

const recordingServer = () => {
    const lines: Array<{ level: string; message: string }> = [];
    const server = {
        log: {
            info: (message: string) => lines.push({ level: 'info', message }),
            warn: (message: string) => lines.push({ level: 'warn', message }),
            error: (message: string) => lines.push({ level: 'error', message }),
            debug: () => undefined,
        },
    } as unknown as FastifyInstance;
    return { server, lines };
};

const MICROVM_CHOSEN: AsrRuntimeConfig = {
    streamingEngineMicrovm: true,
    virtualParticipantEngineMicrovm: true,
    diarizeVirtualParticipant: true,
};

const meeting = (fields: Partial<CallMetaData> = {}): CallMetaData =>
    ({ callId: 'call-1', ...fields }) as CallMetaData;

test('a MicroVM engine choice uses Amazon Transcribe where no ASR launcher is deployed', () => {
    const { server, lines } = recordingServer();
    assert.equal(resolveAsrEngine(meeting(), server, MICROVM_CHOSEN), 'transcribe');
    // Expected on every meeting of such a deployment, so not a warning.
    assert.deepEqual(
        lines.map((line) => line.level),
        ['info']
    );
});

test('a client that names the MicroVM engine on such a deployment is warned and still transcribed', () => {
    const { server, lines } = recordingServer();
    assert.equal(
        resolveAsrEngine(meeting({ asrEngine: 'microvm' }), server, MICROVM_CHOSEN),
        'transcribe'
    );
    assert.deepEqual(
        lines.map((line) => line.level),
        ['warn']
    );
});

test('a deployment switched to Amazon Transcribe logs nothing', () => {
    const { server, lines } = recordingServer();
    assert.equal(
        resolveAsrEngine(meeting(), server, { ...MICROVM_CHOSEN, streamingEngineMicrovm: false }),
        'transcribe'
    );
    assert.equal(lines.length, 0);
});
