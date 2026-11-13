/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading a meeting chat message as a consent reply, and the other pure rules
 * of the Virtual Participant's consent flow that need no AWS and no page.
 *
 * A consent reply is matched as the whole message, as the VP's chat commands
 * are (details.ts `matchesChatCommand`): case, punctuation and spacing are
 * ignored, and an "LMA" addressee may stand anywhere in it. So "I consent", "i
 * consent!" and "LMA, I consent" are consent, and "I agree with Bo's point" is
 * ordinary chat. That is stricter than the reply classifier in the
 * configuration layer (lma_consent.classify_reply), which looks for a phrase
 * anywhere in a reply: a meeting chat is full of sentences that contain "I
 * agree" without being about the recording, and reading one as consent would
 * start capture.
 *
 * An objection may also be part of a sentence, in the direction of stopping: a
 * message of up to a sentence's length that contains an objection phrase as
 * whole words ("LMA, please stop recording now", "I object to this meeting
 * being recorded", "I agree with Sam, but I don't consent to being recorded")
 * is an objection, addressed to LMA or not. Reading more messages as
 * objections can only stop capture, never start it. Only the text outside
 * quotation marks is searched, so a phrase quoted to tell people how to reply
 * ('If you do not agree, reply "I object".') is not an objection, while the
 * phrase typed on its own, quoted or not, still is. Two kinds of message are
 * not searched at all: a longer one, which explains the phrases rather than
 * using them (another bot's notice), and an `LMA ask` question, which is a
 * question for the meeting assistant, whatever it mentions.
 *
 * Besides the configured phrases, "LMA consent" is always consent and "LMA no
 * consent" and "LMA object" are always objections, so a participant can reply
 * in a form that cannot be mistaken for conversation whatever the deployment
 * configured. Objections are looked for first, so "LMA I do not consent" is an
 * objection although it contains "consent".
 */

export type ConsentDecision = 'consent' | 'objection';
export type ConsentMode = 'off' | 'notify' | 'opt_in';
export type ConsentRule = 'each_participant' | 'owner_attests';

export const CONSENT_MODES: readonly ConsentMode[] = ['off', 'notify', 'opt_in'];
/** From least to most strict, as in lma_consent.RULES. */
export const CONSENT_RULES: readonly ConsentRule[] = ['owner_attests', 'each_participant'];

/** Always consent when addressed to LMA, whatever is configured. */
export const BUILT_IN_CONSENT_PHRASES: readonly string[] = ['consent'];
/** Always an objection when addressed to LMA, whatever is configured. */
export const BUILT_IN_OBJECTION_PHRASES: readonly string[] = ['no consent', 'object'];

/** Longest message that is searched for an objection phrase anywhere in it. */
const MAX_OBJECTION_SENTENCE_LENGTH = 200;

/**
 * Text as replies are compared: Unicode-normalised, lower-cased, and every run
 * of anything that is not a letter or a digit made one space, apostrophes
 * included, so "don't", "don’t" and "dont" are told apart only by the last.
 * Letters of every script are kept, so a phrase configured in another language
 * matches.
 */
export function normalizeReplyText(text: string): string {
    return text
        .normalize('NFKC')
        .toLowerCase()
        .replace(/[^\p{L}\p{N}]+/gu, ' ')
        .trim();
}

/** Comma-separated phrases as a list, trimmed, empty ones dropped (lma_consent.split_phrases). */
export function splitPhrases(value: unknown): string[] {
    const parts = Array.isArray(value) ? value.map(String) : typeof value === 'string' ? value.split(',') : [];
    return parts.map((part) => part.trim()).filter((part) => part.length > 0);
}

/**
 * Text between quotation marks: double quotes straight or curly, guillemets,
 * corner brackets, and single quotes whose closing mark is not followed by a
 * letter, so the apostrophe in "don't" closes nothing.
 */
const QUOTED =
    /["“”„«»「」『』][^"“”„«»「」『』]+["“”„«»「」『』]|(?:^|[^\p{L}\p{N}])['‘].+?['’](?=$|[^\p{L}\p{N}])/gu;

function containsPhrase(words: string, phrase: string): boolean {
    return phrase.length > 0 && ` ${words} `.includes(` ${phrase} `);
}

/**
 * `consent`, `objection` or null for one chat message.
 *
 * Pass the message body only, with any sender prefix the platform adds
 * removed: the same string each platform hands to the chat command matchers.
 */
export function classifyChatReply(
    message: string,
    consentPhrases: readonly string[],
    objectionPhrases: readonly string[],
): ConsentDecision | null {
    if (!message) return null;
    const whole = normalizeReplyText(message);
    if (!whole) return null;
    const tokens = whole.split(' ');
    const addressee = tokens.indexOf('lma');
    const addressed = addressee !== -1;
    const rest = addressed ? [...tokens.slice(0, addressee), ...tokens.slice(addressee + 1)].join(' ') : whole;
    const objections = objectionPhrases.map(normalizeReplyText).filter(Boolean);
    const consents = consentPhrases.map(normalizeReplyText).filter(Boolean);

    // The contains test is held to a sentence's length, so a long message
    // that merely explains the phrases (another bot's notice, say) is not
    // read as somebody objecting. It searches only the text outside quotation
    // marks, so a phrase quoted to tell people how to reply ('reply "I
    // object" to decline') is not read as one either.
    const unquoted =
        message.trim().length <= MAX_OBJECTION_SENTENCE_LENGTH
            ? normalizeReplyText(message.replace(QUOTED, ' '))
                  .split(' ')
                  .filter((token) => token !== 'lma')
                  .join(' ')
            : '';
    const contained = objections.some((phrase) => containsPhrase(unquoted, phrase));

    if (addressed) {
        // Nor is a question for the meeting assistant ("LMA ask what do we
        // say if a client asks us to stop recording?"), which is answered as
        // one: only the exact forms are objections there.
        const question = rest === 'ask' || rest.startsWith('ask ');
        if (BUILT_IN_OBJECTION_PHRASES.includes(rest) || objections.includes(rest) || (contained && !question)) {
            return 'objection';
        }
        if (BUILT_IN_CONSENT_PHRASES.includes(rest) || consents.includes(rest)) {
            return 'consent';
        }
        return null;
    }
    // Unaddressed, a sentence that contains an objection phrase is an
    // objection too. Consent stays exact.
    if (objections.includes(whole)) return 'objection';
    if (consents.includes(whole)) return 'consent';
    if (contained) return 'objection';
    return null;
}

/**
 * `consent`, `objection` or null for one final utterance heard in the meeting.
 *
 * Only an utterance addressed to LMA is a reply: people say "I agree" and
 * "I don't agree" in conversation far more often than they type them, so the
 * unaddressed sentence that is an objection in the chat is ordinary speech.
 * Addressed, it is read as an addressed chat message is
 * (classifyChatReply): "LMA consent", or LMA plus a consent phrase and nothing
 * else ("LMA, I consent"), is consent; "LMA object", "LMA no consent", LMA plus
 * an objection phrase, or an addressed sentence of up to 200 characters that
 * contains an objection phrase, is an objection. A bare phrase is not.
 *
 * Speech recognition often writes "LMA" as its letters ("L M A", "L.M.A."),
 * so a run of those three single letters is read as "LMA" before matching.
 */
export function classifySpokenReply(
    utterance: string,
    consentPhrases: readonly string[],
    objectionPhrases: readonly string[],
): ConsentDecision | null {
    if (!utterance) return null;
    const spelled = utterance.replace(/(^|[^\p{L}\p{N}])l[\s.]*m[\s.]*a(?=$|[^\p{L}\p{N}])/giu, '$1LMA');
    if (!normalizeReplyText(spelled).split(' ').includes('lma')) return null;
    return classifyChatReply(spelled, consentPhrases, objectionPhrases);
}

/** A display name as replies are matched to participants by (lma_consent.normalize_name). */
export function normalizeParticipantName(name: string | null | undefined): string {
    if (typeof name !== 'string') return '';
    return name.normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
}

/** Same rule as lma_consent.render_notice: the four placeholders, anything else as written. */
export function renderNotice(
    template: string,
    values: { lmaUser?: string; meeting?: string; consentPhrase?: string; objectionPhrase?: string },
): string {
    const replacements: Record<string, string> = {
        '{LMA_USER}': values.lmaUser || 'The meeting organizer',
        '{meeting}': values.meeting || 'this meeting',
        '{consent_phrase}': values.consentPhrase || '',
        '{objection_phrase}': values.objectionPhrase || '',
    };
    return (template || '')
        .replace(/\{LMA_USER\}|\{meeting\}|\{consent_phrase\}|\{objection_phrase\}/g, (key) => replacements[key])
        .slice(0, 2000);
}

/** Longest chat message posted in one piece. Under every platform's limit. */
export const MAX_CHAT_MESSAGE_LENGTH = 900;

/**
 * The notice as chat messages: one line each, because Chime and Webex post by
 * typing into the composer, where a newline is an Enter key (see chat-help.ts),
 * and split at sentence ends into messages of at most MAX_CHAT_MESSAGE_LENGTH.
 */
export function noticeChatMessages(notice: string, limit: number = MAX_CHAT_MESSAGE_LENGTH): string[] {
    const oneLine = (notice || '').replace(/\s*[\r\n]+\s*/g, ' ').replace(/\s+/g, ' ').trim();
    if (!oneLine) return [];
    if (oneLine.length <= limit) return [oneLine];
    const sentences = oneLine.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [oneLine];
    const messages: string[] = [];
    let current = '';
    for (const sentence of sentences) {
        const piece = sentence.trim();
        if (!piece) continue;
        if (current && current.length + 1 + piece.length > limit) {
            messages.push(current);
            current = '';
        }
        if (piece.length > limit) {
            for (let i = 0; i < piece.length; i += limit) messages.push(piece.slice(i, i + limit));
            continue;
        }
        current = current ? `${current} ${piece}` : piece;
    }
    if (current) messages.push(current);
    return messages;
}

/** How strict a mode and rule are, as lma_consent.strictness ranks them. */
export function strictness(mode: ConsentMode, rule: ConsentRule | null | undefined): [number, number] {
    const modeRank = Math.max(0, CONSENT_MODES.indexOf(mode));
    const ruleRank = mode === 'opt_in' && rule && CONSENT_RULES.includes(rule) ? CONSENT_RULES.indexOf(rule) : -1;
    return [modeRank, ruleRank];
}

/** Whether `a` is at least as strict as `b`. */
export function atLeastAsStrict(a: [number, number], b: [number, number]): boolean {
    return a[0] > b[0] || (a[0] === b[0] && a[1] >= b[1]);
}

export function isConsentMode(value: unknown): value is ConsentMode {
    return typeof value === 'string' && (CONSENT_MODES as readonly string[]).includes(value);
}

export function isConsentRule(value: unknown): value is ConsentRule {
    return typeof value === 'string' && (CONSENT_RULES as readonly string[]).includes(value);
}
