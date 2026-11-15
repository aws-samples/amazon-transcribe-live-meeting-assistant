/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// A deployment without the ASR MicroVM engine: no launcher and no direct
// endpoint. Read at import time, and the other ASR tests configure an endpoint,
// so this needs a file of its own.
delete process.env.ASR_ENGINE;
delete process.env.ASR_LAUNCHER_FUNCTION_ARN;
delete process.env.ASR_DIRECT_ENDPOINT;
process.env.AWS_REGION = process.env.AWS_REGION || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';

// Dynamic, so the assignments above apply before the module reads them (ESM
// hoists static imports).
const { resolveVpAsrEngine } = await import('./asr-microvm-client.js');

const MICROVM_CHOSEN = { virtualParticipantEngineMicrovm: true, diarizeVirtualParticipant: true };

test('a MicroVM engine choice uses Amazon Transcribe where no ASR launcher is deployed', () => {
    assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN), 'transcribe');
});

test('a per-VP MicroVM override on such a deployment still transcribes with Amazon Transcribe', () => {
    assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, 'microvm'), 'transcribe');
    assert.equal(resolveVpAsrEngine({ ...MICROVM_CHOSEN, virtualParticipantEngineMicrovm: false }, 'microvm'), 'transcribe');
});
