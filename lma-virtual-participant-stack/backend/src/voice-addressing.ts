/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether a participant's utterance is addressed to the voice assistant by its
 * name, as opposed to merely mentioning it.
 *
 * In wake-phrase assistant mode an activated assistant answers only what is
 * addressed to it (wake-reply-gate.ts). The wake phrases ("hey alex") are one
 * way to address it; this module is the other. Participants say "Alex, can
 * you ..." or "..., Alex?" at least as often as "Hey Alex", and the prompt
 * tells the assistant, and through it the participants, that its name works.
 *
 * Where the name comes from: the system prompt's own "You are Alex" (or "Your
 * name is Alex") — the same text that tells the model who it is — and the
 * name the configured wake phrases share (stop-request.ts
 * `assistantNameFromWakePhrases`: "hey alex", "ok alex" -> "alex"). Both are
 * accepted, because a deployment may rename the assistant in one and not the
 * other. chat-addressing.ts is not reused: it matches a VP's display name and
 * "LMA" handles in typed chat, which is a different name and a different
 * grammar from a spoken vocative.
 *
 * The rule. An utterance addresses the assistant by name when the name
 *
 * 1. opens the utterance, perhaps after filler, and is followed by a comma, a
 *    request word, or nothing ("Alex, what's next?", "alex what's next", "um
 *    alex summarize this", "Alex?"). A name opening a statement about someone
 *    ("Alex is presenting", "alex sent the deck", "Alex's slides") is not;
 * 2. is followed by a comma ("Awesome. Alex, the weather?"), or by a request
 *    word ("... alex can you tell me", "alex what is", "alex please"),
 *    unless the word before it makes it the object of a sentence about
 *    someone ("I told Alex, ...", "ask alex what ...", "thanks to alex");
 * 3. closes a sentence after a comma, as a vocative ("What's the weather,
 *    Alex?", "Thanks, Alex.").
 *
 * Everything else that contains the name is a mention, and is not addressed:
 * "I told Alex about it", "Alex will present next", "that was Alex's idea".
 *
 * Rule 3 and the comma in rule 2 need punctuation, which the participants'
 * transcript (Amazon Transcribe, the ASR MicroVM) has and Nova Sonic's own
 * transcript of a turn does not. Nova's lowercase turns are covered by rules 1
 * and 2's request words; an unpunctuated "what's the weather alex" is not
 * addressed on Nova's transcript, but the participants' transcript of the same
 * speech ("What's the weather, Alex?") is, and the agent reads both.
 */

import { addressesAssistant } from './muted-lookup.js';
import { assistantNameFromWakePhrases, foldWakeSpellings } from './stop-request.js';

/**
 * How close together two transcripts of one participant's speech must be to
 * count as the same utterance: two consecutive Nova turns that its turn
 * detection split at a pause, or a Nova turn and the participants' transcript
 * of the same speech.
 */
export const UTTERANCE_GAP_MS = 3000;

/** Words that, before the name, make it the object of a sentence about someone. */
const MENTION_BEFORE_NAME = new Set([
    'to', 'told', 'tell', 'telling', 'ask', 'asked', 'asking', 'with', 'and', 'or', 'for', 'from',
    'about', 'of', 'by', 'like', 'call', 'called', 'named', 'said', 'that', 'than', 'is', 'was',
    'saw', 'see', 'met', 'meet', 'emailed', 'email', 'pinged', 'ping', 'cc', 'via', 'but', 'let',
    'where', 'if', 'whether', 'because', 'thanks to', 'the',
]);

/** Words that, after the name, begin a request put to it. Two-word entries are
 *  matched as a pair, so "alex can you" is a request and "alex can do it" is not. */
const REQUEST_AFTER_NAME = [
    'can you', 'could you', 'would you', 'will you', 'do you', 'did you', 'are you', 'have you',
    'is there', 'are there', 'please', 'what', 'what\'s', 'whats', 'how', 'how\'s', 'when', 'where',
    'where\'s', 'who', 'who\'s', 'why', 'which', 'tell', 'give', 'find', 'search', 'look', 'check',
    'show', 'list', 'summarize', 'summarise', 'explain', 'remind', 'read', 'help', 'repeat',
    'translate', 'stop', 'mute', 'go ahead', 'go', 'switch', 'turn', 'change', 'set', 'start', 'take',
    'make', 'add', 'note', 'write', 'draft', 'send', 'pull', 'compare', 'recap',
];

/** Fillers that may come before a name that opens the utterance. */
const LEADING_FILLER = new Set(['um', 'uh', 'er', 'erm', 'hmm', 'oh', 'so', 'well', 'ok', 'yo', 'now', 'right']);

/** Words after "You are" that are not a name ("You are an AI assistant"). */
const NOT_A_NAME = new Set([
    'a', 'an', 'the', 'now', 'not', 'here', 'in', 'on', 'at', 'my', 'our', 'your', 'this', 'that',
    'ai', 'amazon', 'going', 'being', 'helping', 'acting', 'part', 'currently', 'very', 'always',
    'never', 'only', 'to', 'also', 'listening', 'joining', 'attending',
]);

/**
 * The assistant's name as the system prompt gives it: the capitalised word in
 * "You are Alex, ..." or the word in "Your name is Alex". Null when the prompt
 * names nothing ("You are an AI meeting assistant").
 */
export function assistantNameFromPrompt(systemPrompt: string | undefined | null): string | null {
    if (!systemPrompt) return null;
    const named = /\byour name is\s+([A-Za-z][A-Za-z'-]*)/i.exec(systemPrompt);
    // "You are Alex": capitalised, so "you are going to" is not a name.
    const youAre = /\b[Yy]ou(?: are|'re)\s+([A-Z][A-Za-z'-]*)/.exec(systemPrompt);
    for (const match of [named, youAre]) {
        const word = match?.[1]?.toLowerCase().replace(/'s$/, '');
        if (word && word.length > 1 && !NOT_A_NAME.has(word)) return word;
    }
    return null;
}

/** Every name the assistant answers to: the prompt's, then the wake phrases'. Lowercase, no duplicates. */
export function assistantNames(systemPrompt: string | undefined | null, wakePhrases: string[]): string[] {
    const names = [assistantNameFromPrompt(systemPrompt), assistantNameFromWakePhrases(wakePhrases)];
    return names.filter((name, i): name is string => !!name && names.indexOf(name) === i);
}

/**
 * Lowercased, with the punctuation the rule reads (commas and sentence ends)
 * kept as their own tokens and everything else that is not a letter, digit or
 * apostrophe turned into a space.
 */
function tokenize(text: string): string[] {
    return foldWakeSpellings(
        text
            .toLowerCase()
            .replace(/[‘’]/g, '\'')
            .replace(/([,.?!;:])/g, ' $1 ')
            // Hyphens inside a word are kept ("mary-kate"), as the prompt's name keeps them.
            .replace(/[^a-z0-9',.?!;:-]+/g, ' ')
            .replace(/(^|[^a-z0-9])-+|-+(?=[^a-z0-9]|$)/g, '$1 ')
            .replace(/\s+/g, ' ')
            .trim(),
    )
        .split(' ')
        .filter((t) => t.length > 0)
        .map((t) => (t === ';' || t === ':' ? '.' : t));
}

const SENTENCE_END = new Set(['.', '?', '!']);
const isWord = (t: string | undefined): t is string => !!t && t !== ',' && !SENTENCE_END.has(t);

function startsRequest(tokens: string[], at: number): boolean {
    const one = tokens[at];
    const two = isWord(tokens[at + 1]) ? `${one} ${tokens[at + 1]}` : '';
    return REQUEST_AFTER_NAME.includes(two) || REQUEST_AFTER_NAME.includes(one);
}

function mentionBefore(tokens: string[], at: number): boolean {
    const one = tokens[at - 1];
    if (!isWord(one)) return false;
    const two = isWord(tokens[at - 2]) ? `${tokens[at - 2]} ${one}` : '';
    return MENTION_BEFORE_NAME.has(one) || MENTION_BEFORE_NAME.has(two);
}

/** Whether `text` addresses the assistant by one of `names` (see the module comment for the rule). */
export function addressedByName(text: string, names: string[]): boolean {
    if (!text || names.length === 0) return false;
    const tokens = tokenize(text);
    const wanted = new Set(names.map((n) => n.toLowerCase()));
    for (let i = 0; i < tokens.length; i++) {
        if (!wanted.has(tokens[i])) continue;
        const next = tokens[i + 1];
        const followedByRequest = next === ',' || (isWord(next) && startsRequest(tokens, i + 1));
        // Rule 1: the name opens the utterance, after nothing but filler, and
        // is followed by a comma, a request word, or nothing at all.
        const opens = tokens.slice(0, i).every((t) => LEADING_FILLER.has(t) || t === ',');
        if (opens) {
            if (followedByRequest || !isWord(next)) return true;
            continue;
        }
        if (mentionBefore(tokens, i)) continue;
        // Rule 2: a comma or a request word after the name.
        if (followedByRequest) return true;
        // Rule 3: ", Alex" closing a sentence or the utterance.
        if (tokens[i - 1] === ',' && (next === undefined || SENTENCE_END.has(next))) return true;
    }
    return false;
}

/** Whether `text` addresses the assistant, by wake phrase or by name. */
export function addressesAssistantByWakeOrName(text: string, wakePhrases: string[], names: string[]): boolean {
    return addressesAssistant(text, wakePhrases) || addressedByName(text, names);
}
