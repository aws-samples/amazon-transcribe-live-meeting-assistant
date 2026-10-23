/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Recognising "stop talking" in a meeting transcript.
 *
 * The voice assistant's primary way of being told to stop is the `mute` tool it
 * calls itself. This module backs that up with a transcript path, so a
 * participant saying "Hey Alex, stop talking" while the assistant is mid-answer
 * is acted on even though the repeated wake phrase alone carries no new intent.
 *
 * The match is deliberately narrow. A false positive silences the assistant
 * until it is next addressed by wake phrase, which may be well beyond the turn
 * that triggered it, so the utterance must be addressed to the assistant (it
 * has to contain a wake phrase) and, once the wake phrase and conversational
 * filler are removed, must consist of nothing but a stop command. "Hey Alex,
 * why did the build stop?" and "Hey Alex, can you stop the recording and
 * summarise the last hour?" both leave extra words behind and are not matched.
 */

export const DEFAULT_WAKE_PHRASES = 'hey alex,ok alex,hi alex,hello alex';

/**
 * Words that carry no intent of their own and are dropped before the remainder
 * is compared against the stop commands, so that "could you please stop now"
 * and "stop" are treated alike.
 */
const FILLER_TOKENS = new Set([
    'a',
    'can',
    'could',
    'er',
    'hello',
    'hey',
    'hi',
    'just',
    'now',
    'ok',
    'okay',
    'please',
    'thank',
    'thanks',
    'uh',
    'um',
    'will',
    'would',
    'you',
    'your',
    'yourself',
]);

/**
 * What is left of an utterance, after the wake phrase and filler are removed,
 * for it to count as a request for silence. Matching is exact: anything with
 * words left over is a different request that happens to mention stopping.
 *
 * Apostrophes are normalised to spaces, so "that's enough" arrives here as
 * "that s enough".
 */
const STOP_COMMANDS = new Set([
    'be quiet',
    'be silent',
    'enough',
    'mute',
    'quiet',
    'shut up',
    'silence',
    'stop',
    'stop it',
    'stop speaking',
    'stop talking',
    'that is enough',
    'that s enough',
]);

/** Lowercases and reduces everything that is not a letter or digit to a single
 *  space, so transcription punctuation and contractions do not affect matching. */
function normalize(text: string): string {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function escapeForRegExp(text: string): string {
    return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * The wake phrases this deployment is configured with, in the order given.
 *
 * Shared so that the transcript stop path and the wake-phrase resumption path
 * work from one list and cannot drift apart as the configured phrases change.
 * Only the list is shared, not the matching: the agents' `containsWakePhrase`
 * strips a fixed set of punctuation, while `isStopTalkingRequest` below reduces
 * every non-alphanumeric character to a space, so it accepts some spellings
 * ("Hey-Alex") that `containsWakePhrase` does not.
 */
export function configuredWakePhrases(env: Record<string, string | undefined> = process.env): string[] {
    const configured = env.VOICE_ASSISTANT_WAKE_PHRASES || DEFAULT_WAKE_PHRASES;
    return configured
        .split(',')
        .map((phrase) => phrase.trim().toLowerCase())
        .filter((phrase) => phrase.length > 0);
}

/**
 * Whether `text` is a participant asking the voice assistant to stop talking.
 *
 * Requires the utterance to address the assistant by wake phrase and to contain
 * no request other than the stop itself. See the module comment for why this is
 * intentionally strict.
 */
export function isStopTalkingRequest(
    text: string,
    wakePhrases: string[] = configuredWakePhrases(),
): boolean {
    const normalized = normalize(text || '');
    if (!normalized) {
        return false;
    }

    let addressedToAssistant = false;
    let remainder = normalized;
    for (const phrase of wakePhrases) {
        const normalizedPhrase = normalize(phrase);
        if (!normalizedPhrase) {
            continue;
        }
        const pattern = escapeForRegExp(normalizedPhrase).replace(/\s+/g, '\\s+');
        // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp -- the phrase is regex-escaped above, so an escaped wake phrase cannot produce catastrophic backtracking
        const occurrences = new RegExp(`\\b${pattern}\\b`, 'g');
        if (!occurrences.test(remainder)) {
            continue;
        }
        addressedToAssistant = true;
        // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp -- same escaped phrase as above
        remainder = remainder.replace(new RegExp(`\\b${pattern}\\b`, 'g'), ' ');
    }

    if (!addressedToAssistant) {
        return false;
    }

    const command = remainder
        .split(/\s+/)
        .filter((token) => token.length > 0 && !FILLER_TOKENS.has(token))
        .join(' ');

    return STOP_COMMANDS.has(command);
}
