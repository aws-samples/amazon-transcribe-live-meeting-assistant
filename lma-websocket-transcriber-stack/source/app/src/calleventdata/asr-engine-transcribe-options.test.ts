/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// A deployment with the ASR MicroVM engine. Read at import time, so set before
// the module under test loads. Nothing here invokes the launcher.
// Any non-empty value marks the engine as deployed.
process.env['ASR_LAUNCHER_FUNCTION_ARN'] = 'fake-asr-launcher';
delete process.env['ASR_DIRECT_ENDPOINT'];
delete process.env['ASR_ENGINE_DEFAULT'];
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';

import { resolveAsrEngine } from './asr-microvm';
import { AsrRuntimeConfig } from './asr-config';
import { CallMetaData } from './eventtypes';
import { TranscriptionSettings } from './session-settings';

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

const SHIPPED_DEFAULT: AsrRuntimeConfig = {
    streamingEngineMicrovm: true,
    virtualParticipantEngineMicrovm: true,
    diarizeVirtualParticipant: false,
};

const PLAIN: TranscriptionSettings = {
    languageCode: 'en-US',
    languageOptions: '',
    preferredLanguage: 'None',
    contentRedactionEnabled: false,
    contentRedactionType: 'PII',
    piiEntityTypes: '',
    customVocabularyName: '',
    customLanguageModelName: '',
    showSpeakerLabel: false,
};

const meeting = (fields: Partial<CallMetaData> = {}): CallMetaData =>
    ({ callId: 'call-1', ...fields }) as CallMetaData;

test('a meeting with no Amazon Transcribe-only option uses the MicroVM engine', () => {
    const { server, lines } = recordingServer();
    assert.equal(resolveAsrEngine(meeting(), server, SHIPPED_DEFAULT, PLAIN), 'microvm');
    assert.equal(lines.length, 0);
});

const TRANSCRIBE_ONLY: Array<[string, Partial<TranscriptionSettings>, RegExp]> = [
    ['content redaction', { contentRedactionEnabled: true }, /content redaction/],
    // The flag is what counts, even where Amazon Transcribe would not redact the language.
    ['content redaction in a language it does not redact', { contentRedactionEnabled: true, languageCode: 'de-DE' }, /content redaction/],
    ['a custom vocabulary', { customVocabularyName: 'product-names' }, /custom vocabulary product-names/],
    ['a custom language model', { customLanguageModelName: 'clm-1' }, /custom language model clm-1/],
    ['language identification', { languageCode: 'identify-language', languageOptions: 'en-US,es-US' }, /language identification \(identify-language\)/],
    [
        'multi-language identification',
        { languageCode: 'identify-multiple-languages', languageOptions: 'en-US,es-US' },
        /language identification \(identify-multiple-languages\)/,
    ],
];

for (const [name, options, reason] of TRANSCRIBE_ONLY) {
    test(`a meeting using ${name} stays on Amazon Transcribe, logged at info with the reason`, () => {
        const { server, lines } = recordingServer();
        assert.equal(resolveAsrEngine(meeting(), server, SHIPPED_DEFAULT, { ...PLAIN, ...options }), 'transcribe');
        assert.equal(lines.length, 1);
        assert.equal(lines[0]?.level, 'info');
        assert.match(lines[0]?.message ?? '', reason);
    });
}

test('a client that names the MicroVM engine does not override an Amazon Transcribe-only option', () => {
    const { server, lines } = recordingServer();
    assert.equal(
        resolveAsrEngine(meeting({ asrEngine: 'microvm' }), server, SHIPPED_DEFAULT, {
            ...PLAIN,
            contentRedactionEnabled: true,
        }),
        'transcribe'
    );
    assert.deepEqual(
        lines.map((line) => line.level),
        ['warn']
    );
});

test('the explicit client choice still wins over the deployment switch when no such option is used', () => {
    const { server } = recordingServer();
    assert.equal(
        resolveAsrEngine(meeting({ asrEngine: 'microvm' }), server, { ...SHIPPED_DEFAULT, streamingEngineMicrovm: false }, PLAIN),
        'microvm'
    );
});

test('a meeting on Amazon Transcribe by choice logs nothing about its options', () => {
    const { server, lines } = recordingServer();
    assert.equal(
        resolveAsrEngine(meeting(), server, { ...SHIPPED_DEFAULT, streamingEngineMicrovm: false }, {
            ...PLAIN,
            contentRedactionEnabled: true,
        }),
        'transcribe'
    );
    assert.equal(lines.length, 0);
});
