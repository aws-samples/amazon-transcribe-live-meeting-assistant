/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The reply to `LMA help`: the chat commands this Virtual Participant answers.
 *
 * Built from the same tables the matchers read — the dismissal verbs and the
 * pause / resume / help command words in details.ts, and the assistant-mode
 * vocabulary in assistant-toggles.ts — so the list cannot advertise a command
 * the matchers do not accept, or leave one out. Each command is listed in its
 * canonical form only; the aliases are in docs/virtual-participant.md and
 * docs/voice-assistant.md.
 *
 * Each message is a single line. Chime and Webex post by typing into the
 * composer, where a newline is an Enter key and would send the list in pieces,
 * and the Zoom and Teams fallbacks type too; one line per message is the one
 * shape every platform's send path delivers intact. The list is split into
 * messages instead, each about the length of the introduction message, which
 * every platform already posts.
 */
import { details, END_COMMAND_VERBS } from './details.js';
import { configuredWakePhrases } from './stop-request.js';
import { ASSISTANT_CHAT_COMMANDS, VOICE_CONTROL_COMMANDS } from './assistant-toggles.js';
import type { VoiceAssistantProvider } from './voice-assistant-interface.js';
import { chatAskAvailable } from './chat-ask.js';
import { consentHelpActive, consentHelpLine } from './consent-help.js';
import { addressingVpHandle, otherVpsPresent, preferredVpHandle } from './vp-handle.js';
import { normalizeChatText } from './chat-text.js';

/**
 * The name to add after "LMA" to address this VP (vp-handle.ts
 * `addressingVpHandle`: the owner part whole when another VP shares the short
 * one), or null when its display name names nobody.
 */
export function addressingHandle(): string | null {
    if (preferredVpHandle(details.scribeIdentity)) return addressingVpHandle(details.scribeIdentity);
    const name = details.scribeIdentity.trim();
    return name && normalizeChatText(name) !== 'lma' ? name : null;
}

/** What the help reply needs to know about the voice assistant. */
export type ChatHelpAssistant = Pick<VoiceAssistantProvider, 'isEnabled' | 'supportsModeChanges'>;

/** A command as participants are told to type it: the addressee, then the
 *  command word, in lower case. */
function typed(command: string): string {
    return `"LMA ${command.toLowerCase()}"`;
}

/** The first configured wake phrase, capitalised as it would be written. */
function spokenWakePhrase(): string {
    const phrase = configuredWakePhrases()[0] || 'hey alex';
    return phrase.replace(/\b\p{L}/gu, (letter) => letter.toUpperCase());
}

/**
 * The chat messages posted in answer to `LMA help`.
 *
 * The assistant-mode commands are listed only when a voice assistant is
 * running that can actually change mode. With no voice assistant, or with one
 * that declines every mode change, those commands would only ever be answered
 * with a refusal, so listing them would be advertising something that does not
 * work in this meeting.
 */
export function chatHelpMessages(
    assistant: ChatHelpAssistant,
    canAsk: boolean = chatAskAvailable(),
    consent: boolean = consentHelpActive(),
    othersPresent: boolean = otherVpsPresent(details.scribeIdentity),
): string[] {
    // `LMA ask` is listed only when a Strands meeting assistant is there to
    // answer it, for the same reason as the mode commands below.
    const ask = canAsk ? `${typed('ask <question>')} to ask the meeting assistant, ` : '';
    const messages = [
        'Live Meeting Assistant chat commands: '
            + `${typed(END_COMMAND_VERBS[0])} to remove me from the meeting, `
            + `${typed(details.pauseCommand)} / ${typed(details.startCommand)} to pause or resume the transcript, `
            + ask
            + `${typed(details.helpCommand)} to show this list.`,
    ];

    // Addressing this VP by name, for a meeting with more than one
    // (chat-addressing.ts). Only while another VP is actually here: in a
    // meeting with one, which is nearly every meeting, the line is noise, and
    // the help reply was the bulk of what participants saw as "a bunch of
    // messages from LMA". Not listed for a display name that names nobody.
    const handle = othersPresent ? addressingHandle() : null;
    if (handle) {
        messages.push(
            `With more than one LMA in the meeting, add my name after "LMA": "LMA ${handle} help", `
                + `or mention me (@${details.scribeIdentity}).`,
        );
    }

    if (assistant.isEnabled() && assistant.supportsModeChanges()) {
        const modes = ASSISTANT_CHAT_COMMANDS.map((entry) => `${typed(entry.phrases[0])} (${entry.summary})`);
        messages.push(`Voice assistant: ${modes.join(', ')}.`);
    }
    if (assistant.isEnabled()) {
        // Muting (assistant-toggles.ts VOICE_CONTROL_COMMANDS) works with any
        // provider; the spoken mode switch (voice-mode-switch.ts) only with
        // one that changes mode. Translator mode is left by a phrase, which
        // needs no wake phrase.
        const [mute, unmute] = VOICE_CONTROL_COMMANDS.map((entry) => typed(entry.phrases[0]));
        const spoken = assistant.supportsModeChanges()
            ? ` Or say "${spokenWakePhrase()}, switch to translator mode" (or assistant, listen mode),`
                + ' and "stop translating" to leave it.'
            : '';
        messages.push(
            `${mute} / ${unmute} to silence the voice assistant or bring it back; `
                + `"${spokenWakePhrase()}, stop" cuts off an answer.${spoken}`,
        );
    }

    // The consent replies, while the meeting runs a consent flow
    // (meeting-consent.ts); a meeting without one has nothing to reply to.
    if (consent) messages.push(consentHelpLine());

    return messages;
}

/**
 * Every message `chatHelpMessages` can post, whichever assistant is running —
 * for the platforms that recognise the VP's own outgoing chat by its text.
 */
export function allChatHelpMessages(): string[] {
    const all: string[] = [];
    const assistants: ChatHelpAssistant[] = [
        { isEnabled: () => true, supportsModeChanges: () => true },
        { isEnabled: () => true, supportsModeChanges: () => false },
        { isEnabled: () => false, supportsModeChanges: () => false },
    ];
    for (const canAsk of [true, false]) {
        for (const consent of [true, false]) {
            for (const othersPresent of [true, false]) {
                for (const assistant of assistants) {
                    all.push(...chatHelpMessages(assistant, canAsk, consent, othersPresent));
                }
            }
        }
    }
    return [...new Set(all)];
}
