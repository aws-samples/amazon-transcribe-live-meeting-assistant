/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Amazon Transcribe options and the audio-recording default of a meeting.
 *
 * Each of these was a stack parameter passed to this task as an environment
 * variable. They are now settings in the Amazon Transcribe and Meeting Recording
 * sections of the Configuration page, and the parameter is the setting's
 * default: the configuration seeder writes its value into the configuration's
 * baseline, an administrator's change wins over it, and the environment variable
 * the task still carries is what a setting falls back to when the configuration
 * does not hold it or cannot be read.
 *
 * Read once per meeting, when it starts, on the sections' `immediate` tier:
 * redaction and recording are privacy-bearing, so every meeting reads them
 * afresh and a saved change applies to the very next one. A meeting keeps what
 * it started with across every Transcribe reconnect.
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { FastifyBaseLogger } from 'fastify';

import {
    ConfigurationStore,
    RECORDING,
    SettingType,
    TRANSCRIPTION,
    parameterBackedSetting,
    transcribeOptionsProblem,
} from '../config/configuration-store';
import { normalizeErrorForLogging } from '../utils/common';

export interface TranscriptionSettings {
    /** A language code, or identify-language / identify-multiple-languages. */
    readonly languageCode: string;
    /** Comma-separated; empty when not set. */
    readonly languageOptions: string;
    /** A language code, or 'None'. */
    readonly preferredLanguage: string;
    /** The flag as set; whether redaction applies also depends on the language. */
    readonly contentRedactionEnabled: boolean;
    readonly contentRedactionType: string;
    /** Comma-separated; empty when not set. */
    readonly piiEntityTypes: string;
    /** Empty for none. */
    readonly customVocabularyName: string;
    /** Empty for none. */
    readonly customLanguageModelName: string;
    /** The deployment default for speaker partitioning; a client's own choice wins. */
    readonly showSpeakerLabel: boolean;
}

export interface RecordingSettings {
    /** The default for recording a meeting's audio; a client's own choice wins. */
    readonly recordAudio: boolean;
}

export interface SessionSettings {
    readonly transcription: TranscriptionSettings;
    readonly recording: RecordingSettings;
}

interface SettingSource {
    readonly envVar: string;
    readonly type: SettingType;
}

/**
 * Each setting's environment variable and type. One entry per line, as
 * `name: { envVar: 'NAME', type: 'kind' },`: the cross-language test in
 * lma-ai-stack/test/test_parameter_backed_settings.py reads this table and holds
 * it to parameter_defaults.json and to this task's environment.
 */
export const TRANSCRIPTION_SETTINGS: Readonly<
    Record<Exclude<keyof TranscriptionSettings, 'contentRedactionType'>, SettingSource>
> = {
    languageCode: { envVar: 'TRANSCRIBE_LANGUAGE_CODE', type: 'string' },
    languageOptions: { envVar: 'TRANSCRIBE_LANGUAGE_OPTIONS', type: 'string' },
    preferredLanguage: { envVar: 'TRANSCRIBE_PREFERRED_LANGUAGE', type: 'string' },
    contentRedactionEnabled: { envVar: 'IS_CONTENT_REDACTION_ENABLED', type: 'boolean' },
    piiEntityTypes: { envVar: 'TRANSCRIBE_PII_ENTITY_TYPES', type: 'string' },
    customVocabularyName: { envVar: 'CUSTOM_VOCABULARY_NAME', type: 'string' },
    customLanguageModelName: { envVar: 'CUSTOM_LANGUAGE_MODEL_NAME', type: 'string' },
    showSpeakerLabel: { envVar: 'SHOW_SPEAKER_LABEL', type: 'boolean' },
};

/** As TRANSCRIPTION_SETTINGS, for the Meeting Recording section. */
export const RECORDING_SETTINGS: Readonly<Record<keyof RecordingSettings, SettingSource>> = {
    recordAudio: { envVar: 'SHOULD_RECORD_CALL', type: 'boolean' },
};

const text = (values: Record<string, unknown>, name: keyof typeof TRANSCRIPTION_SETTINGS): string =>
    parameterBackedSetting(values, name, 'string', process.env[TRANSCRIPTION_SETTINGS[name].envVar], '');

const flag = (
    values: Record<string, unknown>,
    source: SettingSource,
    name: string,
    fallback = false
): boolean => parameterBackedSetting(values, name, 'boolean', process.env[source.envVar], fallback);

/**
 * The settings from the two sections' resolved values, each falling back to its
 * environment variable. The three that a request cannot go without fall back
 * further to what this task has always used when its variable is unset.
 */
export const sessionSettingsFrom = (
    transcription: Record<string, unknown>,
    recording: Record<string, unknown>
): SessionSettings => ({
    transcription: {
        languageCode: text(transcription, 'languageCode') || 'en-US',
        languageOptions: text(transcription, 'languageOptions'),
        preferredLanguage: text(transcription, 'preferredLanguage') || 'None',
        contentRedactionEnabled: flag(
            transcription,
            TRANSCRIPTION_SETTINGS.contentRedactionEnabled,
            'contentRedactionEnabled'
        ),
        // A stack parameter only: PII is the one value it allows, so there is
        // nothing for the page to choose.
        contentRedactionType: process.env['CONTENT_REDACTION_TYPE'] || 'PII',
        piiEntityTypes: text(transcription, 'piiEntityTypes'),
        customVocabularyName: text(transcription, 'customVocabularyName'),
        customLanguageModelName: text(transcription, 'customLanguageModelName'),
        showSpeakerLabel: flag(transcription, TRANSCRIPTION_SETTINGS.showSpeakerLabel, 'showSpeakerLabel'),
    },
    recording: {
        recordAudio: flag(recording, RECORDING_SETTINGS.recordAudio, 'recordAudio'),
    },
});

/**
 * Why a meeting's options cannot be used together, or undefined when they can:
 * the deploy-time combination checks, shared with the Virtual Participant (see
 * transcribeOptionsProblem).
 */
export const transcriptionProblem = (settings: TranscriptionSettings): string | undefined =>
    transcribeOptionsProblem(settings);

/** The languages Amazon Transcribe redacts while streaming. */
const REDACTED_LANGUAGES: ReadonlySet<string> = new Set(['en-US', 'en-AU', 'en-GB', 'es-US']);

/**
 * Whether redaction is requested for a session: the flag is set and the language
 * is one Amazon Transcribe redacts while streaming. What is sent is what is billed.
 */
export const isRedactionApplied = (settings: TranscriptionSettings): boolean =>
    settings.contentRedactionEnabled && REDACTED_LANGUAGES.has(settings.languageCode);

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';

/** A section of a resolved body, or an empty object when it holds something else. */
const sectionOf = (body: Record<string, unknown>, name: string): Record<string, unknown> => {
    const value = body[name];
    return value !== null && typeof value === 'object' && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : {};
};

/** One per process. Both sections are on the immediate tier, so it caches nothing. */
let store: ConfigurationStore | undefined;

/** Exposed for tests: forget the store. */
export const resetSessionSettingsStore = (): void => {
    store = undefined;
};

/** Exposed for tests: read through this store instead of building one. */
export const useSessionSettingsStore = (replacement: ConfigurationStore): void => {
    store = replacement;
};

/** The settings the deploy-time combination check covers; see transcriptionProblem. */
const COMBINED_SETTINGS = [
    'languageCode',
    'languageOptions',
    'preferredLanguage',
    'customVocabularyName',
    'customLanguageModelName',
] as const;

/**
 * The settings for a meeting that is starting.
 *
 * Read once per meeting by its caller and kept for the meeting. Both sections
 * are on the immediate tier, because redaction and recording are privacy-bearing:
 * every meeting reads them afresh, and a save applies to the very next one.
 *
 * Never throws. A configuration table that cannot be read is logged and this
 * task's environment answers -- the stack parameters' values, redaction and
 * recording included -- because losing a meeting's transcript over a settings
 * lookup would be far worse than starting it with the deployment's own values.
 * The next meeting reads again.
 *
 * When the saved options fail the deploy-time combination checks, only the
 * options those checks cover give way to the environment's, which passed them
 * when the stack deployed; the page's redaction choices and the rest stand.
 */
export const getSessionSettings = async (log: FastifyBaseLogger): Promise<SessionSettings> => {
    const tableName = process.env['CONFIGURATION_TABLE_NAME'] || '';
    if (!tableName && !store) {
        return sessionSettingsFrom({}, {});
    }
    let transcriptionValues: Record<string, unknown>;
    let recordingValues: Record<string, unknown>;
    try {
        store = store ?? new ConfigurationStore({ tableName, client: new DynamoDBClient({ region: AWS_REGION }) });
        // Both sections are on the immediate tier, so they are taken from one
        // consistent read of the resolved document: three GetItems rather than
        // the six two getSection calls would make, and one saved state rather
        // than two that a save between them could split.
        const document = await store.getDocument(true);
        transcriptionValues = sectionOf(document.body, TRANSCRIPTION.name);
        recordingValues = sectionOf(document.body, RECORDING.name);
    } catch (error) {
        log.warn(
            '[SETTINGS]: could not read the Amazon Transcribe and recording settings from the configuration ' +
                `table; using the stack parameter values of this task, redaction and recording included: ${normalizeErrorForLogging(error)}`
        );
        return sessionSettingsFrom({}, {});
    }
    const settings = sessionSettingsFrom(transcriptionValues, recordingValues);
    const problem = transcriptionProblem(settings.transcription);
    if (!problem) {
        return settings;
    }
    log.warn(
        `[SETTINGS]: the Amazon Transcribe options saved on the Configuration page cannot be used: ${problem}. ` +
            'Using the stack parameter values of this deployment for the language, language options, preferred ' +
            'language, custom vocabulary and custom language model until they are corrected.'
    );
    const withoutCombined = { ...transcriptionValues };
    for (const name of COMBINED_SETTINGS) {
        delete withoutCombined[name];
    }
    return sessionSettingsFrom(withoutCombined, recordingValues);
};
