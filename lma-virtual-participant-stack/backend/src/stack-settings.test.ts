/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Configuration page's values reach the Virtual Participant's environment.
 *
 * - A value the configuration holds replaces the task's variable; one it does not
 *   hold leaves the variable as the task was started with it (the parameter's).
 * - Redaction is derived as the template derives it: the flag, and en-US.
 * - Amazon Transcribe options that fail the deploy-time combination checks are
 *   left alone as a whole, while the Virtual Participant's own settings still
 *   apply.
 * - A table that cannot be read changes nothing.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import type { ConfigurationStore } from './configuration-store.js';
import { applyConfiguredSettings, applySections } from './stack-settings.js';

const started = (): Record<string, string | undefined> => ({
    TRANSCRIBE_LANGUAGE_CODE: 'en-US',
    TRANSCRIBE_LANGUAGE_OPTIONS: '',
    TRANSCRIBE_PREFERRED_LANGUAGE: 'None',
    CUSTOM_VOCABULARY_NAME: 'deployed-vocabulary',
    ENABLE_CONTENT_REDACTION: 'false',
    ENABLE_AUDIO_RECORDING: 'true',
    VOICE_ASSISTANT_PROVIDER: 'none',
    VOICE_ASSISTANT_ACTIVATION_MODE: 'wake_phrase',
    VOICE_ASSISTANT_ACTIVATION_DURATION: '30',
    VP_ATTENDEE_POLL_MS: '20000',
});

test('a value the configuration holds replaces the variable, as text', () => {
    const env = started();
    const result = applySections(
        {
            virtual_participant: {
                voiceAssistantProvider: 'amazon_nova_sonic',
                voiceAssistantActivationMode: 'always_active',
                voiceAssistantActivationDuration: 60,
                attendeePollMs: 10000,
            },
            recording: { recordAudio: false },
            transcription: {},
        },
        env,
    );
    assert.equal(env['VOICE_ASSISTANT_PROVIDER'], 'amazon_nova_sonic');
    assert.equal(env['VOICE_ASSISTANT_ACTIVATION_MODE'], 'always_active');
    assert.equal(env['VOICE_ASSISTANT_ACTIVATION_DURATION'], '60');
    assert.equal(env['VP_ATTENDEE_POLL_MS'], '10000');
    assert.equal(env['ENABLE_AUDIO_RECORDING'], 'false');
    assert.deepEqual(result.notes, []);
});

test('the display name is applied with its {LMA_USER} placeholder kept for details.ts to fill', () => {
    const env = { ...started(), LMA_IDENTITY: 'LMA ({LMA_USER})' };
    const result = applySections({ virtual_participant: { displayName: 'LMA Notetaker ({LMA_USER})' } }, env);
    assert.equal(env['LMA_IDENTITY'], 'LMA Notetaker ({LMA_USER})');
    assert.ok(result.applied.includes('LMA_IDENTITY'));

    const unchanged = { ...started(), LMA_IDENTITY: 'LMA ({LMA_USER})' };
    applySections({ virtual_participant: {} }, unchanged);
    assert.equal(unchanged['LMA_IDENTITY'], 'LMA ({LMA_USER})');
});

test('the meeting chat switch is applied as text, and left unset when the configuration has no value', () => {
    const env = started();
    applySections({ virtual_participant: { captureMeetingChat: false } }, env);
    assert.equal(env['CAPTURE_MEETING_CHAT'], 'false');
    const untouched = started();
    applySections({ virtual_participant: {} }, untouched);
    assert.equal(untouched['CAPTURE_MEETING_CHAT'], undefined);
});

test('a setting the configuration does not hold keeps the task value', () => {
    const env = started();
    const result = applySections({ virtual_participant: {}, recording: {}, transcription: {} }, env);
    assert.deepEqual(result.applied, []);
    assert.deepEqual(env, started());
});

test('a stored value of the wrong type is passed over', () => {
    const env = started();
    applySections({ virtual_participant: { voiceAssistantActivationDuration: 'long' } }, env);
    assert.equal(env['VOICE_ASSISTANT_ACTIVATION_DURATION'], '30');
});

test('redaction is requested only for en-US, as the template derives it', () => {
    const english = started();
    applySections({ transcription: { contentRedactionEnabled: true } }, english);
    assert.equal(english['ENABLE_CONTENT_REDACTION'], 'true');

    const spanish = started();
    applySections({ transcription: { contentRedactionEnabled: true, languageCode: 'es-US' } }, spanish);
    assert.equal(spanish['TRANSCRIBE_LANGUAGE_CODE'], 'es-US');
    assert.equal(spanish['ENABLE_CONTENT_REDACTION'], 'false');
});

test('the redaction flag is also kept as saved, whatever the language, for the meeting chat', () => {
    const spanish = started();
    applySections({ transcription: { contentRedactionEnabled: true, languageCode: 'es-US' } }, spanish);
    assert.equal(spanish['ENABLE_CONTENT_REDACTION'], 'false');
    assert.equal(spanish['CONTENT_REDACTION_REQUESTED'], 'true');

    const off = started();
    applySections({ transcription: { contentRedactionEnabled: false } }, off);
    assert.equal(off['CONTENT_REDACTION_REQUESTED'], 'false');

    const unset = started();
    applySections({ transcription: {} }, unset);
    assert.equal(unset['CONTENT_REDACTION_REQUESTED'], undefined);
});

test('options that cannot be used together give way, while the page redaction still applies', () => {
    const env = started();
    const result = applySections(
        {
            // identify-language with the deployed custom vocabulary still set.
            transcription: {
                languageCode: 'identify-language',
                languageOptions: 'en-US, es-US',
                contentRedactionEnabled: true,
            },
            virtual_participant: { voiceAssistantProvider: 'elevenlabs' },
        },
        env,
    );
    assert.equal(env['TRANSCRIBE_LANGUAGE_CODE'], 'en-US');
    assert.equal(env['TRANSCRIBE_LANGUAGE_OPTIONS'], '');
    // The deployed language is en-US, which the Virtual Participant redacts.
    assert.equal(env['ENABLE_CONTENT_REDACTION'], 'true');
    assert.equal(env['VOICE_ASSISTANT_PROVIDER'], 'elevenlabs');
    assert.match(result.notes.join(' '), /custom vocabulary cannot be used with identify-language/);
});

test('options that can be used together are applied together', () => {
    const env = started();
    applySections(
        {
            transcription: {
                languageCode: 'identify-language',
                languageOptions: 'en-US, es-US',
                customVocabularyName: '',
            },
        },
        env,
    );
    assert.equal(env['TRANSCRIBE_LANGUAGE_CODE'], 'identify-language');
    assert.equal(env['CUSTOM_VOCABULARY_NAME'], '');
});

const fakeStore = (sections: Record<string, Record<string, unknown>> | 'fail') =>
    ({
        getDocument: async (consistentRead: boolean) => {
            assert.equal(consistentRead, true, 'the immediate-tier sections are read consistently');
            if (sections === 'fail') {
                throw new Error('AccessDeniedException');
            }
            return { body: sections, baselineBody: sections, profileBody: {} };
        },
    }) as unknown as ConfigurationStore;

test('the three sections are read and applied', async () => {
    const env = started();
    const result = await applyConfiguredSettings(
        env,
        fakeStore({ virtual_participant: { simliFaceId: 'face-1' }, recording: { recordAudio: false } }),
    );
    assert.equal(env['SIMLI_FACE_ID'], 'face-1');
    assert.equal(env['ENABLE_AUDIO_RECORDING'], 'false');
    assert.deepEqual(result.applied.sort(), ['ENABLE_AUDIO_RECORDING', 'SIMLI_FACE_ID']);
});

test('a table that cannot be read changes nothing', async () => {
    const env = started();
    const result = await applyConfiguredSettings(env, fakeStore('fail'));
    assert.deepEqual(result.applied, []);
    assert.deepEqual(env, started());
    assert.match(result.notes.join(' '), /could not read the configuration table/);
});

test('with no table named nothing is read', async () => {
    const env = started();
    const result = await applyConfiguredSettings(env);
    assert.deepEqual(result.applied, []);
    assert.deepEqual(env, started());
});

test('Record Virtual Participant meeting video off overrides a launch that asked for video', () => {
    const env: Record<string, string | undefined> = { ...started(), ENABLE_VIDEO_RECORDING: 'true', DEPLOYMENT_VIDEO_RECORDING: 'true' };
    const result = applySections({ recording: { recordVpMeetingVideo: false } }, env);
    assert.equal(env['ENABLE_VIDEO_RECORDING'], 'false');
    assert.equal(env['VIDEO_RECORDING_OFF_REASON'], 'configuration');
    assert.ok(result.applied.includes('ENABLE_VIDEO_RECORDING'));
    assert.match(result.notes.join(' '), /Record Virtual Participant meeting video is off/);
});

test('with the setting on, the launch decides, and a launch without video stays without', () => {
    const asked: Record<string, string | undefined> = { ...started(), ENABLE_VIDEO_RECORDING: 'true', DEPLOYMENT_VIDEO_RECORDING: 'true' };
    const on = applySections({ recording: { recordVpMeetingVideo: true } }, asked);
    assert.equal(asked['ENABLE_VIDEO_RECORDING'], 'true');
    assert.equal(asked['VIDEO_RECORDING_OFF_REASON'], undefined);
    assert.ok(!on.applied.includes('ENABLE_VIDEO_RECORDING'));

    const declined: Record<string, string | undefined> = { ...started(), ENABLE_VIDEO_RECORDING: 'false', DEPLOYMENT_VIDEO_RECORDING: 'true' };
    applySections({ recording: { recordVpMeetingVideo: true } }, declined);
    assert.equal(declined['ENABLE_VIDEO_RECORDING'], 'false');
    assert.equal(declined['VIDEO_RECORDING_OFF_REASON'], undefined, 'the user chose it, so no reason is recorded');
});

test('a deployment that does not record video overrides the launch and the setting', () => {
    const env: Record<string, string | undefined> = { ...started(), ENABLE_VIDEO_RECORDING: 'true', DEPLOYMENT_VIDEO_RECORDING: 'false' };
    applySections({ recording: { recordVpMeetingVideo: true } }, env);
    assert.equal(env['ENABLE_VIDEO_RECORDING'], 'false');
    assert.equal(env['VIDEO_RECORDING_OFF_REASON'], 'deployment');
});

test('the deployment switch applies even when the table cannot be read; the setting then reads as on', async () => {
    const off: Record<string, string | undefined> = { ...started(), ENABLE_VIDEO_RECORDING: 'true', DEPLOYMENT_VIDEO_RECORDING: 'false' };
    const result = await applyConfiguredSettings(off, fakeStore('fail'));
    assert.equal(off['ENABLE_VIDEO_RECORDING'], 'false');
    assert.equal(off['VIDEO_RECORDING_OFF_REASON'], 'deployment');
    assert.deepEqual(result.applied, ['ENABLE_VIDEO_RECORDING']);

    const on: Record<string, string | undefined> = { ...started(), ENABLE_VIDEO_RECORDING: 'true', DEPLOYMENT_VIDEO_RECORDING: 'true' };
    await applyConfiguredSettings(on, fakeStore('fail'));
    assert.equal(on['ENABLE_VIDEO_RECORDING'], 'true');
});

test('the meeting retention is applied, and never raised past the stack parameter', () => {
    const shorter = { ...started(), METERING_EXPIRATION_DAYS: '90' };
    const applied = applySections({ retention: { meetingRecordExpirationInDays: 14 } }, shorter);
    assert.equal(shorter['METERING_EXPIRATION_DAYS'], '14');
    assert.ok(applied.applied.includes('METERING_EXPIRATION_DAYS'));

    const longer = { ...started(), METERING_EXPIRATION_DAYS: '7' };
    applySections({ retention: { meetingRecordExpirationInDays: 30 } }, longer);
    assert.equal(longer['METERING_EXPIRATION_DAYS'], '7');

    const unset = { ...started(), METERING_EXPIRATION_DAYS: '0.5' };
    const none = applySections({ retention: {} }, unset);
    assert.equal(unset['METERING_EXPIRATION_DAYS'], '0.5');
    assert.ok(!none.applied.includes('METERING_EXPIRATION_DAYS'));
});
