/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Configuration page's values for the settings this Virtual Participant
 * reads from its environment, applied once, when it starts.
 *
 * These settings were each a stack parameter, passed to the task as an
 * environment variable: the Amazon Transcribe options and the audio-recording
 * default (the Amazon Transcribe and Meeting Recording sections, which the
 * WebSocket transcriber reads too), and the Virtual Participant's own voice
 * assistant and its activation mode, avatar and Teams meeting-end detection
 * (the Virtual Participant section). The parameter is now each setting's
 * default: the configuration seeder writes its value into the configuration's
 * baseline, and an administrator's change wins over it.
 *
 * Rather than change every place that reads one of these variables, this module
 * reads the three sections and sets each variable the configuration holds a
 * value for, before anything reads it. A variable the configuration does not
 * hold, and every variable when the table cannot be read, keeps the value the
 * task was started with, which is the parameter's.
 *
 * Some of those readers run when their module is imported: `details.ts` builds
 * the meeting details, and `voice-assistant.ts`, `simli-avatar.ts` and
 * `scribe.ts` build their singletons (the transcription service reads the wake
 * phrases as it is constructed). Each of them imports this module first, whose top-level `await` makes
 * ES module evaluation finish this read before theirs starts. On both launch
 * paths the process starts when the meeting does -- an ECS task at task start, a
 * MicroVM's app when its run payload arrives -- so what is applied is what is
 * saved at that moment.
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';

import {
    ConfigurationStore,
    RECORDING,
    SettingType,
    TRANSCRIPTION,
    VIRTUAL_PARTICIPANT,
    coerceSetting,
    transcribeOptionsProblem,
} from './configuration-store.js';

interface SettingTarget {
    readonly section: string;
    readonly setting: string;
    readonly envVar: string;
    readonly type: SettingType;
}

/**
 * Each setting, and the environment variable it is applied to. One entry per
 * line, as `{ section: 's', setting: 'n', envVar: 'NAME', type: 'kind' },`: the
 * cross-language test in lma-ai-stack/test/test_parameter_backed_settings.py
 * reads this table and holds it to parameter_defaults.json and to the task
 * definition's environment.
 */
export const VIRTUAL_PARTICIPANT_SETTINGS: readonly SettingTarget[] = [
    { section: 'transcription', setting: 'languageCode', envVar: 'TRANSCRIBE_LANGUAGE_CODE', type: 'string' },
    { section: 'transcription', setting: 'languageOptions', envVar: 'TRANSCRIBE_LANGUAGE_OPTIONS', type: 'string' },
    { section: 'transcription', setting: 'preferredLanguage', envVar: 'TRANSCRIBE_PREFERRED_LANGUAGE', type: 'string' },
    { section: 'transcription', setting: 'customVocabularyName', envVar: 'CUSTOM_VOCABULARY_NAME', type: 'string' },
    { section: 'recording', setting: 'recordAudio', envVar: 'ENABLE_AUDIO_RECORDING', type: 'boolean' },
    { section: 'virtual_participant', setting: 'voiceAssistantProvider', envVar: 'VOICE_ASSISTANT_PROVIDER', type: 'string' },
    { section: 'virtual_participant', setting: 'voiceAssistantActivationMode', envVar: 'VOICE_ASSISTANT_ACTIVATION_MODE', type: 'string' },
    { section: 'virtual_participant', setting: 'voiceAssistantWakePhrases', envVar: 'VOICE_ASSISTANT_WAKE_PHRASES', type: 'string' },
    { section: 'virtual_participant', setting: 'voiceAssistantActivationDuration', envVar: 'VOICE_ASSISTANT_ACTIVATION_DURATION', type: 'integer' },
    { section: 'virtual_participant', setting: 'elevenLabsAgentId', envVar: 'ELEVENLABS_AGENT_ID', type: 'string' },
    { section: 'virtual_participant', setting: 'simliFaceId', envVar: 'SIMLI_FACE_ID', type: 'string' },
    { section: 'virtual_participant', setting: 'attendeePollMs', envVar: 'VP_ATTENDEE_POLL_MS', type: 'integer' },
    { section: 'virtual_participant', setting: 'pollsBeforeEnd', envVar: 'VP_POLLS_BEFORE_END', type: 'integer' },
    { section: 'virtual_participant', setting: 'pollsBeforeEndMissing', envVar: 'VP_POLLS_BEFORE_END_MISSING', type: 'integer' },
];

/**
 * The variable whose value is derived rather than copied: whether redaction is
 * requested is the flag combined with the language, which is how the template
 * sets it (ShouldEnableContentRedaction in template.yaml). The Virtual
 * Participant redacts only en-US.
 */
export const CONTENT_REDACTION_VARIABLE = 'ENABLE_CONTENT_REDACTION';
const REDACTED_LANGUAGES: ReadonlySet<string> = new Set(['en-US']);

export interface AppliedSettings {
    /** The variables set from the configuration. */
    readonly applied: string[];
    /** Why nothing, or less than everything, was applied; empty when all was. */
    readonly notes: string[];
}

type Sections = Record<string, Record<string, unknown>>;

/** The settings the deploy-time combination check covers; see transcribeOptionsProblem. */
const COMBINED_SETTINGS: ReadonlySet<string> = new Set([
    'languageCode',
    'languageOptions',
    'preferredLanguage',
    'customVocabularyName',
]);

/**
 * Sets each variable the sections hold a value for. Pure over its inputs, so it
 * is what the tests exercise; `applyConfiguredSettings` reads the sections.
 *
 * Amazon Transcribe options saved on the page that fail the deploy-time
 * combination checks would stop the meeting being transcribed, so the options
 * those checks cover are left alone, and the task's own values, which passed
 * them when the stack deployed, stand. The page's redaction choices still apply,
 * as they do in the WebSocket transcriber.
 */
export const applySections = (sections: Sections, env: Record<string, string | undefined>): AppliedSettings => {
    const applied: string[] = [];
    const notes: string[] = [];
    const transcription = sections['transcription'] ?? {};

    const value = (target: SettingTarget): string | undefined => {
        const stored = coerceSetting(target.type, (sections[target.section] ?? {})[target.setting]);
        return stored === undefined ? undefined : String(stored);
    };
    const effective = (setting: string, envVar: string): string =>
        value({ section: 'transcription', setting, envVar, type: 'string' }) ?? env[envVar] ?? '';
    const problem = transcribeOptionsProblem({
        languageCode: effective('languageCode', 'TRANSCRIBE_LANGUAGE_CODE') || 'en-US',
        languageOptions: effective('languageOptions', 'TRANSCRIBE_LANGUAGE_OPTIONS'),
        preferredLanguage: effective('preferredLanguage', 'TRANSCRIBE_PREFERRED_LANGUAGE') || 'None',
        customVocabularyName: effective('customVocabularyName', 'CUSTOM_VOCABULARY_NAME'),
        customLanguageModelName: '',
    });
    if (problem) {
        notes.push(
            `the Amazon Transcribe options saved on the Configuration page cannot be used: ${problem}; ` +
                'using the stack parameter values of this deployment for them',
        );
    }

    for (const target of VIRTUAL_PARTICIPANT_SETTINGS) {
        if (problem && target.section === 'transcription' && COMBINED_SETTINGS.has(target.setting)) {
            continue;
        }
        const configured = value(target);
        if (configured !== undefined) {
            env[target.envVar] = configured;
            applied.push(target.envVar);
        }
    }

    const redactionFlag = coerceSetting('boolean', transcription['contentRedactionEnabled']);
    if (redactionFlag !== undefined) {
        const language = env['TRANSCRIBE_LANGUAGE_CODE'] || 'en-US';
        env[CONTENT_REDACTION_VARIABLE] = String(redactionFlag && REDACTED_LANGUAGES.has(language));
        applied.push(CONTENT_REDACTION_VARIABLE);
    }
    return { applied, notes };
};

/**
 * Reads the three sections and applies them to `env`.
 *
 * Never throws: a Virtual Participant that cannot read its configuration joins
 * its meeting with the values it was started with, which is what it always did.
 */
export const applyConfiguredSettings = async (
    env: Record<string, string | undefined> = process.env,
    store?: ConfigurationStore,
): Promise<AppliedSettings> => {
    const tableName = env['CONFIGURATION_TABLE_NAME'] || '';
    if (!tableName && !store) {
        return { applied: [], notes: ['no configuration table is named'] };
    }
    try {
        const reader =
            store ??
            new ConfigurationStore({
                tableName,
                client: new DynamoDBClient({ region: env['AWS_REGION'] || 'us-east-1' }),
            });
        // All three sections are on the immediate tier, so they are taken from one
        // consistent read of the resolved document -- the reads getSection would
        // make for each of them, once -- rather than three.
        const document = await reader.getDocument(true);
        const sectionOf = (name: string): Record<string, unknown> => {
            const value = document.body[name];
            return value !== null && typeof value === 'object' && !Array.isArray(value)
                ? (value as Record<string, unknown>)
                : {};
        };
        return applySections(
            {
                [VIRTUAL_PARTICIPANT.name]: sectionOf(VIRTUAL_PARTICIPANT.name),
                [TRANSCRIPTION.name]: sectionOf(TRANSCRIPTION.name),
                [RECORDING.name]: sectionOf(RECORDING.name),
            },
            env,
        );
    } catch (error) {
        return {
            applied: [],
            notes: [`could not read the configuration table; using the values this task started with: ${String(error)}`],
        };
    }
};

/** What was applied when this module was first imported; see the module comment. */
export const appliedStackSettings: AppliedSettings = await applyConfiguredSettings();

console.log(
    `[SETTINGS] Configuration page values applied: ${appliedStackSettings.applied.join(', ') || 'none'}` +
        (appliedStackSettings.notes.length ? ` (${appliedStackSettings.notes.join('; ')})` : ''),
);
