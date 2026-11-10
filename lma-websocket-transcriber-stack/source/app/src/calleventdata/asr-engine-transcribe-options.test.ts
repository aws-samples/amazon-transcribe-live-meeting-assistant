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
// No published bundle list unless a test passes one: English only.
delete process.env['ASR_MODEL_LANGUAGES'];
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';

import { resolveAsrEngine } from './asr-microvm';
import { AsrRuntimeConfig } from './asr-config';
import { CallMetaData } from './eventtypes';
import { TranscriptionSettings } from './session-settings';
import { isMicrovmLanguage, parseBundleLanguages } from '../config/configuration-store';

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
    diarizeVirtualParticipant: true,
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

for (const languageCode of ['en-US', 'en-GB', 'en-AU', '']) {
    test(`a meeting in English (${languageCode || 'unset'}) uses the MicroVM engine`, () => {
        const { server, lines } = recordingServer();
        assert.equal(resolveAsrEngine(meeting(), server, SHIPPED_DEFAULT, { ...PLAIN, languageCode }), 'microvm');
        assert.equal(lines.length, 0);
    });
}

const TRANSCRIBE_ONLY: Array<[string, Partial<TranscriptionSettings>, RegExp]> = [
    ['content redaction', { contentRedactionEnabled: true }, /content redaction/],
    // The flag is what counts, even where Amazon Transcribe would not redact the language.
    ['content redaction in a language it does not redact', { contentRedactionEnabled: true, languageCode: 'de-DE' }, /content redaction/],
    ['a custom vocabulary', { customVocabularyName: 'product-names' }, /custom vocabulary product-names/],
    ['a custom language model', { customLanguageModelName: 'clm-1' }, /custom language model clm-1/],
    ['language identification', { languageCode: 'identify-language', languageOptions: 'en-US,es-US' }, /language identification \(identify-language\)/],
    ['a language other than English (de-DE) with no published bundle list', { languageCode: 'de-DE' }, /language de-DE/],
    ['a language other than English (es-US) with no published bundle list', { languageCode: 'es-US' }, /language es-US/],
    ['a language other than English (ja-JP) with no published bundle list', { languageCode: 'ja-JP' }, /language ja-JP/],
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

// The default bundle's languages as the ASR stack publishes them: those
// Parakeet TDT 0.6B v3 and the Nemotron 3.5 preview have in common.
const PARAKEET_V3_BUNDLE = parseBundleLanguages('bg,cs,da,de,en,es,et,fi,fr,hr,hu,it,nl,pl,pt,ro,ru,sk,sv,uk');

for (const languageCode of ['es-US', 'es-ES', 'de-DE', 'fr-FR', 'pt-BR']) {
    test(`a meeting in ${languageCode} uses the MicroVM engine when the deployed bundle transcribes it`, () => {
        const { server, lines } = recordingServer();
        assert.equal(
            resolveAsrEngine(meeting(), server, SHIPPED_DEFAULT, { ...PLAIN, languageCode }, PARAKEET_V3_BUNDLE),
            'microvm'
        );
        assert.equal(lines.length, 0);
    });
}

test('a meeting in a language the deployed bundle does not transcribe stays on Amazon Transcribe', () => {
    const { server, lines } = recordingServer();
    assert.equal(
        resolveAsrEngine(meeting(), server, SHIPPED_DEFAULT, { ...PLAIN, languageCode: 'ja-JP' }, PARAKEET_V3_BUNDLE),
        'transcribe'
    );
    assert.match(lines[0]?.message ?? '', /language ja-JP/);
});

test('language identification stays on Amazon Transcribe whatever the bundle transcribes', () => {
    for (const languageCode of ['identify-language', 'identify-multiple-languages']) {
        assert.equal(
            resolveAsrEngine(
                meeting(),
                undefined,
                SHIPPED_DEFAULT,
                { ...PLAIN, languageCode, languageOptions: 'en-US,es-US' },
                PARAKEET_V3_BUNDLE
            ),
            'transcribe'
        );
    }
});

test('the bundle list is read from ASR_MODEL_LANGUAGES', () => {
    process.env['ASR_MODEL_LANGUAGES'] = 'en,es';
    try {
        assert.equal(
            resolveAsrEngine(meeting(), undefined, SHIPPED_DEFAULT, { ...PLAIN, languageCode: 'es-US' }),
            'microvm'
        );
        assert.equal(
            resolveAsrEngine(meeting(), undefined, SHIPPED_DEFAULT, { ...PLAIN, languageCode: 'de-DE' }),
            'transcribe'
        );
    } finally {
        delete process.env['ASR_MODEL_LANGUAGES'];
    }
});

test('isMicrovmLanguage compares the base language with the bundle list', () => {
    assert.equal(isMicrovmLanguage('es-US', PARAKEET_V3_BUNDLE), true);
    assert.equal(isMicrovmLanguage('de-DE', PARAKEET_V3_BUNDLE), true);
    assert.equal(isMicrovmLanguage('ja-JP', PARAKEET_V3_BUNDLE), false);
    assert.equal(isMicrovmLanguage('el-GR', PARAKEET_V3_BUNDLE), false);
});

test('isMicrovmLanguage takes English only when no bundle list is published', () => {
    for (const unknown of [[], parseBundleLanguages(''), parseBundleLanguages(undefined)]) {
        assert.equal(isMicrovmLanguage('en-US', unknown), true);
        assert.equal(isMicrovmLanguage('en-GB', unknown), true);
        assert.equal(isMicrovmLanguage('', unknown), true);
        assert.equal(isMicrovmLanguage('es-US', unknown), false);
        assert.equal(isMicrovmLanguage('de-DE', unknown), false);
    }
});

test('isMicrovmLanguage takes English even from a list that omits it', () => {
    assert.equal(isMicrovmLanguage('en-AU', parseBundleLanguages('es,fr')), true);
});

test('parseBundleLanguages reduces each entry to a lower-case base code', () => {
    assert.deepEqual(parseBundleLanguages(' en, ES-us ,,fr '), ['en', 'es', 'fr']);
});
