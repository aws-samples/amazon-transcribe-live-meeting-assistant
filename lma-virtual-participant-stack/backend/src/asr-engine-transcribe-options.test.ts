/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// A deployment with the ASR MicroVM engine: any non-empty launcher marks it as
// deployed, and nothing here invokes it. Read at import time, so set first.
process.env.ASR_LAUNCHER_FUNCTION_ARN = 'fake-asr-launcher';
delete process.env.ASR_DIRECT_ENDPOINT;
delete process.env.ASR_ENGINE;
// No published bundle list unless a test passes one: English only.
delete process.env.ASR_MODEL_LANGUAGES;
delete process.env.TRANSCRIBE_LANGUAGE_CODE;
delete process.env.CUSTOM_VOCABULARY_NAME;
delete process.env.CONTENT_REDACTION_REQUESTED;
delete process.env.ENABLE_CONTENT_REDACTION;
process.env.AWS_REGION = process.env.AWS_REGION || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';

// Dynamic, so the assignments above apply before the module reads them (ESM
// hoists static imports).
const { resolveVpAsrEngine, vpTranscribeOnlySettings } = await import('./asr-microvm-client.js');
const { isMicrovmLanguage, parseBundleLanguages } = await import('./configuration-store.js');

const MICROVM_CHOSEN = { virtualParticipantEngineMicrovm: true, diarizeVirtualParticipant: true };
const PLAIN = {
    languageCode: 'en-US',
    contentRedactionEnabled: false,
    customVocabularyName: '',
    customLanguageModelName: '',
};

test('a VP with no Amazon Transcribe-only option uses the MicroVM engine', () => {
    assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, undefined, PLAIN), 'microvm');
    // And with the options read from an environment that sets none.
    assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, undefined), 'microvm');
});

const TRANSCRIBE_ONLY: Array<[string, Partial<typeof PLAIN>]> = [
    ['content redaction', { contentRedactionEnabled: true }],
    ['a custom vocabulary', { customVocabularyName: 'product-names' }],
    ['a custom language model', { customLanguageModelName: 'clm-1' }],
    ['language identification', { languageCode: 'identify-language' }],
    ['multi-language identification', { languageCode: 'identify-multiple-languages' }],
    ['a language other than English (de-DE) with no published bundle list', { languageCode: 'de-DE' }],
    ['a language other than English (es-US) with no published bundle list', { languageCode: 'es-US' }],
    ['a language other than English (ja-JP) with no published bundle list', { languageCode: 'ja-JP' }],
];

for (const languageCode of ['en-US', 'en-GB', 'en-AU']) {
    test(`a VP in English (${languageCode}) uses the MicroVM engine`, () => {
        assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, languageCode }), 'microvm');
    });
}

test("a VP whose environment names a language other than English stays on Amazon Transcribe", () => {
    assert.equal(
        resolveVpAsrEngine(MICROVM_CHOSEN, undefined, vpTranscribeOnlySettings({ TRANSCRIBE_LANGUAGE_CODE: 'fr-FR' })),
        'transcribe',
    );
});

for (const [name, options] of TRANSCRIBE_ONLY) {
    test(`a VP using ${name} stays on Amazon Transcribe`, () => {
        assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, ...options }), 'transcribe');
    });
}

test('a per-VP MicroVM override does not override an Amazon Transcribe-only option', () => {
    assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, 'microvm', { ...PLAIN, contentRedactionEnabled: true }), 'transcribe');
    // Without one, the override still wins over the deployment switch.
    assert.equal(
        resolveVpAsrEngine({ ...MICROVM_CHOSEN, virtualParticipantEngineMicrovm: false }, 'microvm', PLAIN),
        'microvm',
    );
});

test("the VP's options are read from the environment the Configuration page's values are applied to", () => {
    assert.deepEqual(vpTranscribeOnlySettings({}), PLAIN);
    // The saved redaction flag counts whatever the language...
    assert.equal(
        vpTranscribeOnlySettings({ CONTENT_REDACTION_REQUESTED: 'true', TRANSCRIBE_LANGUAGE_CODE: 'de-DE' }).contentRedactionEnabled,
        true,
    );
    // ...and the task's combined flag where the saved one is not known.
    assert.equal(vpTranscribeOnlySettings({ ENABLE_CONTENT_REDACTION: 'true' }).contentRedactionEnabled, true);
    assert.equal(vpTranscribeOnlySettings({ CUSTOM_VOCABULARY_NAME: 'v' }).customVocabularyName, 'v');
    assert.equal(
        vpTranscribeOnlySettings({ TRANSCRIBE_LANGUAGE_CODE: 'identify-language' }).languageCode,
        'identify-language',
    );
    assert.equal(
        resolveVpAsrEngine(MICROVM_CHOSEN, undefined, vpTranscribeOnlySettings({ CUSTOM_VOCABULARY_NAME: 'v' })),
        'transcribe',
    );
});

// The default bundle's languages as the ASR stack publishes them: those
// Parakeet TDT 0.6B v3 and the Nemotron 3.5 preview have in common.
const PARAKEET_V3_BUNDLE = parseBundleLanguages('bg,cs,da,de,en,es,et,fi,fr,hr,hu,it,nl,pl,pt,ro,ru,sk,sv,uk');

for (const languageCode of ['es-US', 'es-ES', 'de-DE', 'fr-FR', 'pt-BR']) {
    test(`a VP in ${languageCode} uses the MicroVM engine when the deployed bundle transcribes it`, () => {
        assert.equal(
            resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, languageCode }, PARAKEET_V3_BUNDLE),
            'microvm',
        );
    });
}

test('a VP in a language the deployed bundle does not transcribe stays on Amazon Transcribe', () => {
    assert.equal(
        resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, languageCode: 'ja-JP' }, PARAKEET_V3_BUNDLE),
        'transcribe',
    );
});

test('a VP using language identification stays on Amazon Transcribe whatever the bundle transcribes', () => {
    for (const languageCode of ['identify-language', 'identify-multiple-languages']) {
        assert.equal(
            resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, languageCode }, PARAKEET_V3_BUNDLE),
            'transcribe',
        );
    }
});

test("the VP's bundle list is read from ASR_MODEL_LANGUAGES", () => {
    process.env.ASR_MODEL_LANGUAGES = 'en,es';
    try {
        assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, languageCode: 'es-US' }), 'microvm');
        assert.equal(resolveVpAsrEngine(MICROVM_CHOSEN, undefined, { ...PLAIN, languageCode: 'de-DE' }), 'transcribe');
    } finally {
        delete process.env.ASR_MODEL_LANGUAGES;
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
