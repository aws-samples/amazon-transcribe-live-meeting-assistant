/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Deciding whether a `strands_agent` lookup requested while the voice assistant
 * is muted is worth running.
 *
 * A muted assistant still hears the whole meeting when it is always listening:
 * `group` mode starts muted and goes back to muted on its own, and a `normal`
 * assistant asked to stop talking stays muted until it is next addressed. The
 * model keeps answering what it hears either way — LMA only drops the audio —
 * and on a live meeting it asked for 31 lookups in four minutes of ordinary
 * team conversation while muted, none of them asked for and every answer
 * discarded. Each one is a Strands Lambda invocation, with the Bedrock usage
 * and the tool calls that brings.
 *
 * So a lookup requested while muted is not run unless the participant's turn
 * that prompted it addressed the assistant. Being addressed is what unmutes it
 * (the agents do that when the user's turn ends, before the model's tool call
 * arrives), so in practice a muted assistant asking for a lookup was not asked
 * anything; the addressed-turn check covers the order of those two events
 * being reversed. A stop request ("Hey Alex, stop") is addressed but leaves
 * the assistant muted, so it does not count.
 *
 * The tool is still offered while muted. Which tools a session has is fixed
 * when the session starts, and the mute state changes many times a session in
 * `group` mode, so withdrawing the tool would mean rebuilding the session on
 * every wake phrase — losing the moment the assistant is addressed to the
 * rebuild, which is exactly when it needs to answer.
 */

import { normalizeChatText as normalize } from './chat-text.js';
import { configuredWakePhrases, isStopTalkingRequest } from './stop-request.js';

/** Why a lookup was not run. */
export type LookupSkipReason = 'not-addressed';

/**
 * The tool result returned to the model in place of a lookup that was not run.
 * Phrased as an instruction, because the model generates a reply to whatever
 * the tool returns, and the reply should be nothing.
 */
export const SKIPPED_LOOKUP_RESULT = {
    status: 'skipped',
    instruction: 'Not run: you are muted and nobody asked you anything. '
        + 'The participants are talking to each other. Stay silent, do not answer, '
        + 'and do not request another lookup until someone addresses you by name.',
};

function escapeForRegExp(text: string): string {
    return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Whether `text` addresses the assistant by one of the configured wake phrases. */
export function addressesAssistant(
    text: string,
    wakePhrases: string[] = configuredWakePhrases(),
): boolean {
    const normalized = normalize(text || '');
    if (!normalized) {
        return false;
    }
    return wakePhrases.some((phrase) => {
        const normalizedPhrase = normalize(phrase);
        if (!normalizedPhrase) {
            return false;
        }
        const pattern = escapeForRegExp(normalizedPhrase).replace(/\s+/g, '\\s+');
        // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp -- the phrase is regex-escaped above, so an escaped wake phrase cannot produce catastrophic backtracking
        return new RegExp(`\\b${pattern}\\b`).test(normalized);
    });
}

/**
 * Why a `strands_agent` lookup must not be run now, or null if it may.
 *
 * @param muted          whether the assistant's audio is currently gated
 * @param triggeringTurn the most recent completed participant turn, which is
 *                       the one the model is responding to
 */
export function mutedLookupSkipReason(
    muted: boolean,
    triggeringTurn: string,
    wakePhrases: string[] = configuredWakePhrases(),
): LookupSkipReason | null {
    if (!muted) {
        return null;
    }
    if (addressesAssistant(triggeringTurn, wakePhrases) && !isStopTalkingRequest(triggeringTurn, wakePhrases)) {
        return null;
    }
    return 'not-addressed';
}

/**
 * The log line for a lookup that was not run. Carries the state that explains
 * the decision, never the utterance or the query.
 */
export function skippedLookupLogLine(reason: LookupSkipReason, meetingMode: string, activationMode: string): string {
    return `[lookup] action=skipped reason=${reason} muted=true mode=${meetingMode} activation=${activationMode}`;
}
