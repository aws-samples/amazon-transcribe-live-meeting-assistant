/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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
 * shape every platform's send path delivers intact. The list is split into two
 * messages instead, each about the length of the introduction message, which
 * every platform already posts.
 */
import { details, END_COMMAND_VERBS } from './details.js';
import { ASSISTANT_CHAT_COMMANDS } from './assistant-toggles.js';
import type { VoiceAssistantProvider } from './voice-assistant-interface.js';

/** What the help reply needs to know about the voice assistant. */
export type ChatHelpAssistant = Pick<VoiceAssistantProvider, 'isEnabled' | 'supportsModeChanges'>;

/** A command as participants are told to type it: the addressee, then the
 *  command word, in lower case. */
function typed(command: string): string {
    return `"LMA ${command.toLowerCase()}"`;
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
export function chatHelpMessages(assistant: ChatHelpAssistant): string[] {
    const messages = [
        'Live Meeting Assistant chat commands: '
            + `${typed(END_COMMAND_VERBS[0])} to remove me from the meeting, `
            + `${typed(details.pauseCommand)} / ${typed(details.startCommand)} to pause or resume the transcript, `
            + `${typed(details.helpCommand)} to show this list.`,
    ];

    if (assistant.isEnabled() && assistant.supportsModeChanges()) {
        const modes = ASSISTANT_CHAT_COMMANDS.map((entry) => `${typed(entry.phrases[0])} (${entry.summary})`);
        messages.push(`Voice assistant: ${modes.join(', ')}.`);
    }

    return messages;
}

/**
 * Every message `chatHelpMessages` can post, whichever assistant is running —
 * for the platforms that recognise the VP's own outgoing chat by its text.
 */
export function allChatHelpMessages(): string[] {
    const withModes = chatHelpMessages({ isEnabled: () => true, supportsModeChanges: () => true });
    const withoutModes = chatHelpMessages({ isEnabled: () => false, supportsModeChanges: () => false });
    return [...new Set([...withModes, ...withoutModes])];
}
