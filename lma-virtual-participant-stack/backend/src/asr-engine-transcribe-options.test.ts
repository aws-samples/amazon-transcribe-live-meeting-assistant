/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// A deployment with the ASR MicroVM engine: any non-empty launcher marks it as
// deployed, and nothing here invokes it. Read at import time, so set first.
process.env.ASR_LAUNCHER_FUNCTION_ARN = 'fake-asr-launcher';
delete process.env.ASR_DIRECT_ENDPOINT;
delete process.env.ASR_ENGINE;
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

const SHIPPED_DEFAULT = { virtualParticipantEngineMicrovm: true, diarizeVirtualParticipant: false };
const PLAIN = {
    languageCode: 'en-US',
    contentRedactionEnabled: false,
    customVocabularyName: '',
    customLanguageModelName: '',
};

test('a VP with no Amazon Transcribe-only option uses the MicroVM engine', () => {
    assert.equal(resolveVpAsrEngine(SHIPPED_DEFAULT, undefined, PLAIN), 'microvm');
    // And with the options read from an environment that sets none.
    assert.equal(resolveVpAsrEngine(SHIPPED_DEFAULT, undefined), 'microvm');
});

const TRANSCRIBE_ONLY: Array<[string, Partial<typeof PLAIN>]> = [
    ['content redaction', { contentRedactionEnabled: true }],
    ['a custom vocabulary', { customVocabularyName: 'product-names' }],
    ['a custom language model', { customLanguageModelName: 'clm-1' }],
    ['language identification', { languageCode: 'identify-language' }],
    ['multi-language identification', { languageCode: 'identify-multiple-languages' }],
    ['a language other than English (de-DE)', { languageCode: 'de-DE' }],
    ['a language other than English (es-US)', { languageCode: 'es-US' }],
    ['a language other than English (ja-JP)', { languageCode: 'ja-JP' }],
];

for (const languageCode of ['en-US', 'en-GB', 'en-AU']) {
    test(`a VP in English (${languageCode}) uses the MicroVM engine`, () => {
        assert.equal(resolveVpAsrEngine(SHIPPED_DEFAULT, undefined, { ...PLAIN, languageCode }), 'microvm');
    });
}

test("a VP whose environment names a language other than English stays on Amazon Transcribe", () => {
    assert.equal(
        resolveVpAsrEngine(SHIPPED_DEFAULT, undefined, vpTranscribeOnlySettings({ TRANSCRIBE_LANGUAGE_CODE: 'fr-FR' })),
        'transcribe',
    );
});

for (const [name, options] of TRANSCRIBE_ONLY) {
    test(`a VP using ${name} stays on Amazon Transcribe`, () => {
        assert.equal(resolveVpAsrEngine(SHIPPED_DEFAULT, undefined, { ...PLAIN, ...options }), 'transcribe');
    });
}

test('a per-VP MicroVM override does not override an Amazon Transcribe-only option', () => {
    assert.equal(resolveVpAsrEngine(SHIPPED_DEFAULT, 'microvm', { ...PLAIN, contentRedactionEnabled: true }), 'transcribe');
    // Without one, the override still wins over the deployment switch.
    assert.equal(
        resolveVpAsrEngine({ ...SHIPPED_DEFAULT, virtualParticipantEngineMicrovm: false }, 'microvm', PLAIN),
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
        resolveVpAsrEngine(SHIPPED_DEFAULT, undefined, vpTranscribeOnlySettings({ CUSTOM_VOCABULARY_NAME: 'v' })),
        'transcribe',
    );
});
