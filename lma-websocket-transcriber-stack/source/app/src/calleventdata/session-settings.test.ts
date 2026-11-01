/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where a meeting's Amazon Transcribe options and recording default come from.
 *
 * - The Amazon Transcribe and Meeting Recording sections decide; this task's
 *   environment variables, which hold the stack parameters' values, answer for a
 *   setting the sections do not hold, and when the table cannot be read.
 * - A failed read never stops a meeting: the meeting uses the environment's
 *   values, and the next meeting reads again.
 * - The options are laid onto the streaming request exactly as the environment
 *   variables used to be: language or identification, redaction only for the
 *   languages Amazon Transcribe redacts, vocabulary and language model when named.
 */
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyBaseLogger } from 'fastify';

import type { ConfigurationStore } from '../config/configuration-store';
import {
    getSessionSettings,
    isRedactionApplied,
    resetSessionSettingsStore,
    sessionSettingsFrom,
    transcriptionProblem,
    useSessionSettingsStore,
} from './session-settings';
import { applyTranscribeOptions, diarizationSettingsFor } from './transcribe';
import type { CallMetaData } from './eventtypes';

const warnings: string[] = [];
const log = {
    info: () => undefined,
    warn: (message: string) => warnings.push(message),
    error: () => undefined,
    debug: () => undefined,
} as unknown as FastifyBaseLogger;

const ENV = {
    TRANSCRIBE_LANGUAGE_CODE: 'en-GB',
    TRANSCRIBE_LANGUAGE_OPTIONS: '',
    TRANSCRIBE_PREFERRED_LANGUAGE: 'None',
    IS_CONTENT_REDACTION_ENABLED: 'false',
    CONTENT_REDACTION_TYPE: 'PII',
    TRANSCRIBE_PII_ENTITY_TYPES: 'NAME,EMAIL',
    CUSTOM_VOCABULARY_NAME: 'env-vocabulary',
    CUSTOM_LANGUAGE_MODEL_NAME: '',
    SHOW_SPEAKER_LABEL: 'false',
    SHOULD_RECORD_CALL: 'true',
};

const withEnv = (overrides: Record<string, string | undefined> = {}): void => {
    for (const [name, value] of Object.entries({ ...ENV, ...overrides })) {
        if (value === undefined) {
            delete process.env[name];
        } else {
            process.env[name] = value;
        }
    }
};

/** A store answering the resolved document from `sections`, or failing every read. */
const fakeStore = (sections: Record<string, Record<string, unknown>> | 'fail') => {
    const reads = { count: 0 };
    const store = {
        getDocument: async (consistentRead: boolean) => {
            reads.count += 1;
            assert.equal(consistentRead, true, 'the immediate-tier sections are read consistently');
            if (sections === 'fail') {
                throw new Error('AccessDeniedException');
            }
            return { body: sections, baselineBody: sections, profileBody: {} };
        },
    } as unknown as ConfigurationStore;
    return { store, reads };
};

test('the environment answers when there is no configuration table', async () => {
    withEnv();
    resetSessionSettingsStore();
    delete process.env['CONFIGURATION_TABLE_NAME'];
    const settings = await getSessionSettings(log);
    assert.equal(settings.transcription.languageCode, 'en-GB');
    assert.equal(settings.transcription.customVocabularyName, 'env-vocabulary');
    assert.equal(settings.recording.recordAudio, true);
});

test('the sections win over the environment', async () => {
    withEnv();
    const { store } = fakeStore({
        transcription: { languageCode: 'es-US', customVocabularyName: '', contentRedactionEnabled: true },
        recording: { recordAudio: false },
    });
    useSessionSettingsStore(store);
    const settings = await getSessionSettings(log);
    assert.equal(settings.transcription.languageCode, 'es-US');
    // An empty name set on the page means "none", over the parameter's vocabulary.
    assert.equal(settings.transcription.customVocabularyName, '');
    assert.equal(settings.transcription.contentRedactionEnabled, true);
    assert.equal(settings.transcription.piiEntityTypes, 'NAME,EMAIL');
    assert.equal(settings.recording.recordAudio, false);
});

test('a stored value of the wrong type is passed over for the environment', () => {
    withEnv({ SHOW_SPEAKER_LABEL: 'true' });
    const settings = sessionSettingsFrom({ showSpeakerLabel: 'sometimes' }, {});
    assert.equal(settings.transcription.showSpeakerLabel, true);
});

test('with neither, a request still gets a language, a preference and a redaction type', () => {
    withEnv({
        TRANSCRIBE_LANGUAGE_CODE: undefined,
        TRANSCRIBE_PREFERRED_LANGUAGE: undefined,
        CONTENT_REDACTION_TYPE: undefined,
        SHOULD_RECORD_CALL: undefined,
    });
    const settings = sessionSettingsFrom({}, {});
    assert.equal(settings.transcription.languageCode, 'en-US');
    assert.equal(settings.transcription.preferredLanguage, 'None');
    assert.equal(settings.transcription.contentRedactionType, 'PII');
    assert.equal(settings.recording.recordAudio, false);
});

test('an unreadable table is the environment answer, and the next meeting reads again', async () => {
    withEnv();
    warnings.length = 0;
    const { store, reads } = fakeStore('fail');
    useSessionSettingsStore(store);
    const first = await getSessionSettings(log);
    const second = await getSessionSettings(log);
    assert.equal(first.transcription.languageCode, 'en-GB');
    assert.equal(second.transcription.languageCode, 'en-GB');
    // Both sections are privacy-bearing and on the immediate tier: a failure is
    // not remembered, so the first meeting after the table recovers reads it.
    assert.equal(reads.count, 2);
    assert.equal(warnings.length, 2);
});

test('redaction is applied only for the languages Amazon Transcribe redacts while streaming', () => {
    withEnv();
    const base = sessionSettingsFrom({ contentRedactionEnabled: true }, {}).transcription;
    assert.equal(isRedactionApplied({ ...base, languageCode: 'en-US' }), true);
    assert.equal(isRedactionApplied({ ...base, languageCode: 'es-US' }), true);
    assert.equal(isRedactionApplied({ ...base, languageCode: 'fr-FR' }), false);
    assert.equal(isRedactionApplied({ ...base, languageCode: 'identify-language' }), false);
    assert.equal(isRedactionApplied({ ...base, contentRedactionEnabled: false, languageCode: 'en-US' }), false);
});

test('the options are laid onto a streaming request as the environment variables were', () => {
    withEnv();
    const settings = sessionSettingsFrom(
        {
            languageCode: 'en-US',
            contentRedactionEnabled: true,
            customVocabularyName: 'v1',
            customLanguageModelName: 'lm1',
        },
        {}
    ).transcription;
    const request: Record<string, unknown> = {};
    applyTranscribeOptions(request as never, settings);
    assert.deepEqual(request, {
        LanguageCode: 'en-US',
        ContentRedactionType: 'PII',
        PiiEntityTypes: 'NAME,EMAIL',
        VocabularyName: 'v1',
        LanguageModelName: 'lm1',
    });
});

test('language identification sends the options and the preference, and no language code', () => {
    withEnv();
    const settings = sessionSettingsFrom(
        {
            languageCode: 'identify-language',
            languageOptions: 'en-US, es-US',
            preferredLanguage: 'es-US',
            customVocabularyName: '',
        },
        {}
    ).transcription;
    const request: Record<string, unknown> = {};
    applyTranscribeOptions(request as never, settings);
    assert.deepEqual(request, {
        IdentifyLanguage: true,
        LanguageOptions: 'en-US,es-US',
        PreferredLanguage: 'es-US',
    });
});

test('the speaker-partitioning default applies only when the client chose nothing', () => {
    const silent = { callId: 'c' } as CallMetaData;
    assert.deepEqual(diarizationSettingsFor(silent, true), {
        diarizeSystemChannel: true,
        diarizeMicChannel: true,
    });
    const chose = { callId: 'c', diarizeSystemChannel: false, diarizeMicChannel: true } as CallMetaData;
    assert.deepEqual(diarizationSettingsFor(chose, true), {
        diarizeSystemChannel: false,
        diarizeMicChannel: true,
    });
});

test('options that cannot be used together are named, as the deploy-time check names them', () => {
    withEnv({ CUSTOM_VOCABULARY_NAME: '' });
    const identify = (values: Record<string, unknown>) =>
        transcriptionProblem(sessionSettingsFrom({ languageCode: 'identify-language', ...values }, {}).transcription);
    assert.equal(identify({ languageOptions: 'en-US, es-US' }), undefined);
    assert.match(identify({ languageOptions: '' }) ?? '', /needs language options/);
    assert.match(identify({ languageOptions: 'en-US, en-GB' }) ?? '', /more than one dialect of 'en'/);
    assert.match(identify({ languageOptions: 'en-US, es-US', preferredLanguage: 'fr-FR' }) ?? '', /not one of/);
    assert.match(identify({ languageOptions: 'en-US, es-US', customVocabularyName: 'v' }) ?? '', /custom vocabulary/);
    assert.match(identify({ languageOptions: 'en-US, es-US', customLanguageModelName: 'm' }) ?? '', /language model/);
    assert.equal(
        transcriptionProblem(sessionSettingsFrom({ languageCode: 'en-US', customVocabularyName: 'v' }, {}).transcription),
        undefined
    );
});

test('saved options that cannot be used together give way, but the page redaction stands', async () => {
    withEnv();
    warnings.length = 0;
    const { store } = fakeStore({
        transcription: {
            languageCode: 'identify-language',
            languageOptions: '',
            customVocabularyName: '',
            contentRedactionEnabled: true,
            piiEntityTypes: 'SSN',
        },
        recording: { recordAudio: false },
    });
    useSessionSettingsStore(store);
    const settings = await getSessionSettings(log);
    // The options the combination check covers come from the deployment...
    assert.equal(settings.transcription.languageCode, 'en-GB');
    assert.equal(settings.transcription.customVocabularyName, 'env-vocabulary');
    // ...and the page's redaction and recording choices are kept.
    assert.equal(settings.transcription.contentRedactionEnabled, true);
    assert.equal(settings.transcription.piiEntityTypes, 'SSN');
    assert.equal(settings.recording.recordAudio, false);
    assert.match(warnings.join('\n'), /cannot be used: identify-language needs language options/);
});
