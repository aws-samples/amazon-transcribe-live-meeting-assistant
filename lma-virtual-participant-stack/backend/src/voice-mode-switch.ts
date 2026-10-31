/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Changing the voice assistant's mode by asking it, out loud.
 *
 * The meeting chat already has commands for this (`LMA translate`, `LMA assist`,
 * `LMA listen`, `LMA always`, `LMA wake`; see assistant-toggles.ts). This is the
 * spoken route to the same change: outside translator mode the Nova Sonic
 * assistant is given a `set_mode` tool, and a call to it is turned into the
 * same chat command and applied through `applyAssistantChatCommand`, so the
 * two routes share the vocabulary, the combination rules, the restoring of the
 * activation mode a translator or listen mode replaced, and the
 * `[assistant-toggle]` log line (with `route=voice`).
 *
 * The assistant says one sentence to confirm. A mode change replaces the model
 * session, so the sentence has to be spoken before the switch, by the session
 * being replaced: the tool result tells the model what to say, and the switch
 * is made once that sentence has been generated. The sentence is worked out
 * here, ahead of the switch, by the same checks `reconfigure` makes — see
 * {@link predictVoiceModeChange} — so the assistant does not announce a switch
 * that is then declined.
 *
 * Translator mode keeps its single `mute` tool, so it is left by a phrase
 * instead: "stop translating", with or without the assistant's name
 * ({@link isStopTranslatingRequest}).
 */

import type { ActivationMode } from './voice-assistant-interface.js';
import type { MeetingMode } from './nova-sonic-config-loader.js';
import {
    AssistantChatCommand,
    describeActivationMode,
    describeMeetingMode,
    parseAssistantChatCommand,
    requiresAlwaysListening,
} from './assistant-toggles.js';

/** The values the `set_mode` tool accepts. */
export const VOICE_MODES = ['assistant', 'translator', 'listen', 'always_listening', 'wake_phrase'] as const;
export type VoiceMode = (typeof VOICE_MODES)[number];

/** The chat command each spoken mode stands for, by its chat phrase. */
const CHAT_PHRASE: Record<VoiceMode, string> = {
    assistant: 'assist',
    translator: 'translate',
    listen: 'group',
    always_listening: 'always',
    wake_phrase: 'wake',
};

export function isVoiceMode(value: unknown): value is VoiceMode {
    return typeof value === 'string' && (VOICE_MODES as readonly string[]).includes(value);
}

/** The chat command a spoken mode asks for: the very one its chat phrase parses to. */
export function voiceModeCommand(mode: VoiceMode): AssistantChatCommand {
    const command = parseAssistantChatCommand(`LMA ${CHAT_PHRASE[mode]}`);
    if (!command) {
        throw new Error(`no chat command for voice mode ${mode}`);
    }
    return command;
}

export const SET_MODE_TOOL_SPEC = {
    toolSpec: {
        name: 'set_mode',
        description:
            'Change how you work in this meeting. Call this ONLY when a participant addressing YOU by name '
            + 'asks you to switch mode: "translator" to translate between the meeting\'s two languages, '
            + '"assistant" to go back to answering questions, "listen" to listen to the meeting and speak only '
            + 'when addressed by name, "always_listening" to listen all the time, or "wake_phrase" to wait for '
            + 'your wake phrase. Do NOT call it when participants mention these modes to each other.',
        inputSchema: {
            json: JSON.stringify({
                type: 'object',
                properties: {
                    mode: { type: 'string', enum: [...VOICE_MODES], description: 'The mode to switch to.' },
                },
                required: ['mode'],
            }),
        },
    },
};

/** The system-prompt section that goes with the tool. */
export const SET_MODE_PROMPT = 'MODE CHANGES:\n'
    + 'You have the set_mode tool. Use it only when someone addressing YOU by name asks you to change mode - '
    + 'for example "switch to translator mode", "go back to assistant mode", "listen mode", "always listen" '
    + 'or "wake mode". Do not say anything before calling it. After calling it, say exactly the one sentence '
    + 'its result gives you, and nothing else.';

/** The tool result for a call nobody addressed to the assistant. */
export const SET_MODE_NOT_ADDRESSED_RESULT = {
    status: 'not_done',
    instruction: 'Not done: a mode change has to be asked for by name. Say nothing and carry on.',
};

export interface ModeState {
    meetingMode: MeetingMode;
    activationMode: ActivationMode;
    /** Whether a mode change is already being applied. */
    reconfiguring: boolean;
}

export type VoiceModePrediction =
    | { result: 'applied' }
    | { result: 'unchanged' }
    | { result: 'declined'; reason: 'in-progress' | 'needs-always-listening' };

/**
 * How `reconfigure` will answer `command` from `state`, by the same checks in
 * the same order: a change already in progress, then no change, then a
 * meeting mode left without the always-listening activation it needs.
 */
export function predictVoiceModeChange(state: ModeState, command: AssistantChatCommand): VoiceModePrediction {
    const changesMeeting = command.meetingMode !== undefined && command.meetingMode !== state.meetingMode;
    const changesActivation = command.activationMode !== undefined && command.activationMode !== state.activationMode;
    if (state.reconfiguring) {
        return { result: 'declined', reason: 'in-progress' };
    }
    if (!changesMeeting && !changesActivation) {
        return { result: 'unchanged' };
    }
    const meetingMode = command.meetingMode ?? state.meetingMode;
    const activationMode = command.activationMode ?? state.activationMode;
    if (requiresAlwaysListening(meetingMode) && activationMode !== 'always_active') {
        return { result: 'declined', reason: 'needs-always-listening' };
    }
    return { result: 'applied' };
}

/** The one sentence the assistant says for a request and how it will end. */
export function voiceModeSentence(
    state: ModeState,
    command: AssistantChatCommand,
    prediction: VoiceModePrediction,
): string {
    if (prediction.result === 'declined') {
        return prediction.reason === 'in-progress'
            ? "I'm still switching modes. Please ask again in a moment."
            : `I have to keep listening in ${describeMeetingMode(state.meetingMode)}. `
                + 'Switch me to assistant mode first.';
    }
    if (command.meetingMode !== undefined) {
        return prediction.result === 'applied'
            ? `Switching to ${describeMeetingMode(command.meetingMode)}.`
            : `I'm already in ${describeMeetingMode(command.meetingMode)}.`;
    }
    const activation = command.activationMode ?? state.activationMode;
    return prediction.result === 'applied'
        ? `Okay, I'm now ${describeActivationMode(activation)}.`
        : `I'm already ${describeActivationMode(activation)}.`;
}

// ---------------------------------------------------------------------------
// Leaving translator mode by voice
// ---------------------------------------------------------------------------

/** The sentence played once translator mode has been left by voice. */
export const STOPPED_TRANSLATING_PHRASE = "Okay, I've stopped translating.";

/** What is left of the utterance, once its addressee and filler are removed,
 *  for it to ask to leave translator mode. Matched exactly. */
const STOP_TRANSLATING_COMMANDS = new Set([
    'stop translating',
    'stop translation',
    'stop the translation',
    'stop the translator',
    'stop interpreting',
    'no more translating',
    'no more translation',
    'end translation',
    'end the translation',
    'end translator mode',
    'exit translator',
    'exit translator mode',
    'exit translation',
    'exit translation mode',
    'leave translator mode',
    'translator off',
    'translation off',
    'turn off translation',
    'turn off the translator',
    'turn off translator mode',
    'assistant mode',
    'switch to assistant mode',
    'go back to assistant mode',
    'back to assistant mode',
]);

const FILLER = new Set(['please', 'now', 'ok', 'okay', 'can', 'could', 'you', 'will', 'would', 'thanks', 'thank']);

function normalize(text: string): string {
    return text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
}

/**
 * Who a translator can be addressed as: each wake phrase, and the name in each
 * translator pause phrase ("alex" from "alex mute", "translator" from
 * "translator mute"), so "Alex, stop translating" works as well as
 * "Hey Alex, stop translating". Only a pause phrase of the form "<name> mute"
 * yields a name; any other is taken whole.
 */
export function translatorAddressees(wakePhrases: string[], mutePhrases: string[]): string[] {
    const names = mutePhrases
        .map((phrase) => normalize(phrase).replace(/\s*\bmute$/, '').trim())
        .filter(Boolean);
    return [...new Set([...wakePhrases.map(normalize), ...names].filter(Boolean))]
        // Longest first, so "hey alex" is removed whole rather than leaving "hey".
        .sort((a, b) => b.length - a.length);
}

/**
 * Whether `text` asks a translator to stop translating: apart from the
 * assistant's name, if it is used, and filler, the utterance says nothing but
 * one of the phrases above.
 *
 * The name is optional because people do not use it: "Stop translating" on its
 * own is how a participant asks, and a translator that waited for "Alex" kept
 * translating and then paused instead. The whole-utterance rule is what keeps
 * ordinary speech out — "we should stop translating the slides" or "Alex,
 * stop translating this part later" is a sentence to translate, not a command.
 */
export function isStopTranslatingRequest(text: string, addressees: string[]): boolean {
    const words = normalize(text || '').split(' ').filter((word) => word && !FILLER.has(word));
    // The name is taken off either end, where it is said ("Alex, stop
    // translating", "stop translating, Alex"), and each form is tried with and
    // without it: "translator" is a name (from "translator mute") and also the
    // last word of "turn off the translator".
    const lead = stripAddressee(words, addressees, 'start');
    const candidates = [words, lead, stripAddressee(words, addressees, 'end'), stripAddressee(lead, addressees, 'end')];
    return candidates.some((candidate) => STOP_TRANSLATING_COMMANDS.has(candidate.join(' ')));
}

function stripAddressee(words: string[], addressees: string[], end: 'start' | 'end'): string[] {
    for (const name of addressees) {
        const nameWords = name.split(' ');
        const n = nameWords.length;
        if (words.length <= n) continue;
        const slice = end === 'start' ? words.slice(0, n) : words.slice(words.length - n);
        if (slice.join(' ') === name) {
            const rest = end === 'start' ? words.slice(n) : words.slice(0, words.length - n);
            return rest.filter((word) => !FILLER.has(word));
        }
    }
    return words;
}

/** Whether `text` contains one of `phrases` as whole words, whatever the
 *  punctuation: "Translator, unmute." contains "translator unmute". */
export function containsTriggerPhrase(text: string, phrases: string[]): boolean {
    const padded = ` ${normalize(text || '')} `;
    return phrases.some((phrase) => {
        const normalized = normalize(phrase);
        return normalized.length > 0 && padded.includes(` ${normalized} `);
    });
}
