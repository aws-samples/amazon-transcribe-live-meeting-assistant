/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading a chat message as a consent reply (consent-matcher.ts): the whole
 * message, as the VP's chat commands are matched, with an "LMA" addressee
 * allowed, and the built-in "LMA consent" / "LMA no consent" forms.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    atLeastAsStrict,
    classifyChatReply,
    classifySpokenReply,
    noticeChatMessages,
    normalizeReplyText,
    renderNotice,
    splitPhrases,
    strictness,
} from './consent-matcher.js';
import { consentHelpLineFor } from './consent-help.js';
import { DEFAULT_CONSENT_SETTINGS } from './meeting-consent.js';

const CONSENT = DEFAULT_CONSENT_SETTINGS.consentPhrases;
const OBJECTION = DEFAULT_CONSENT_SETTINGS.objectionPhrases;
const classify = (message: string) => classifyChatReply(message, CONSENT, OBJECTION);
const classifySpoken = (utterance: string) => classifySpokenReply(utterance, CONSENT, OBJECTION);

test('a spoken reply counts only when addressed to LMA', () => {
    assert.equal(classifySpoken('LMA, I consent.'), 'consent');
    assert.equal(classifySpoken('I agree, LMA'), 'consent');
    assert.equal(classifySpoken('LMA consent'), 'consent');
    assert.equal(classifySpoken('LMA, stop recording!'), 'objection');
    assert.equal(classifySpoken('LMA object'), 'objection');
    assert.equal(classifySpoken('LMA, I do not consent.'), 'objection', 'objections first');
    for (const bare of ['I consent.', 'I agree', 'Stop recording!', 'I object', "I don't agree.", 'I do not consent.']) {
        assert.equal(classifySpoken(bare), null, bare);
    }
});

test('an addressed spoken consent must be the whole utterance; an addressed objection may be a sentence', () => {
    assert.equal(classifySpoken('LMA, I agree with what Bo said about the roadmap.'), null);
    assert.equal(classifySpoken('LMA, can you please stop recording now?'), 'objection');
    assert.equal(classifySpoken(`LMA ${'and then we talked about the plan '.repeat(8)}stop recording`), null, 'over 200 characters');
    assert.equal(classifySpoken('Can we record the demo again later?'), null);
});

test('LMA spelled out by speech recognition is read as the LMA addressee', () => {
    assert.equal(classifySpoken('L M A consent.'), 'consent');
    assert.equal(classifySpoken('L.M.A. no consent'), 'objection');
    assert.equal(classifySpoken('LMA, please stop recording now.'), 'objection');
    assert.equal(classifySpoken('Elma consent'), null, 'only the three letters');
    assert.equal(classifySpoken('Film a consent'), null);
});

test('a configured consent phrase is consent as the whole message, in any case and punctuation', () => {
    for (const message of ['I consent', 'i consent!', '  I CONSENT. ', 'I agree', 'I accept', 'LMA, I consent', '@LMA I agree', 'I consent LMA']) {
        assert.equal(classify(message), 'consent', message);
    }
});

test('LMA consent is consent and LMA no consent and LMA object are objections whatever is configured', () => {
    assert.equal(classifyChatReply('LMA consent', [], []), 'consent');
    assert.equal(classifyChatReply('lma: consent', [], []), 'consent');
    assert.equal(classifyChatReply('LMA no consent', [], []), 'objection');
    assert.equal(classifyChatReply('LMA object', [], []), 'objection');
    assert.equal(classifyChatReply('consent', [], []), null, 'unaddressed "consent" is ordinary chat');
});

test('a configured objection phrase is an objection as the whole message', () => {
    for (const message of ['I object', 'I do not consent', "I don't consent", 'I don’t consent', 'stop recording', 'Do not record.', 'LMA I do not consent']) {
        assert.equal(classify(message), 'objection', message);
    }
});

test('objections are looked for first', () => {
    assert.equal(classify('LMA I do not consent'), 'objection');
    assert.equal(classify('LMA no consent'), 'objection');
});

test('a sentence that merely contains a consent phrase is ordinary chat', () => {
    for (const message of [
        "I agree with Bo's point",
        'I consent to the budget',
        'I accept the action item',
        'LMA consent is a good feature',
        'Reply "I consent" to agree, or "I object" to decline.',
        '',
        '   ',
    ]) {
        assert.equal(classify(message), null, message);
    }
});

test('an unaddressed sentence containing an objection phrase is an objection', () => {
    for (const message of [
        'I object to this meeting being recorded',
        'i object to this meeting being recorded.',
        'Please stop recording now',
        "Sorry, but I don't consent to that.",
        'I do not agree to being transcribed',
        "I don't agree with the roadmap",
        'please do not record the demo twice',
    ]) {
        assert.equal(classify(message), 'objection', message);
    }
});

test('an objection phrase counts only as whole words', () => {
    assert.equal(classify('I objected to the timeline last week'), null);
    assert.equal(classify('the stop recordings folder is shared'), null);
});

test('an unaddressed sentence containing a consent phrase is not consent', () => {
    assert.equal(classify('I agree with Sam that the demo went well'), null);
    assert.equal(classify('I consent to the new budget'), null);
});

test('a sentence with both a consent and an objection phrase is an objection', () => {
    assert.equal(classify("I agree with Sam, but I don't consent to being recorded"), 'objection');
    assert.equal(classify('I accept that, but please stop recording'), 'objection');
    assert.equal(classify('I agree, but I object to the recording'), 'objection');
});

test('an objection phrase inside quotation marks is not searched for', () => {
    for (const message of [
        'Reply "I consent" to agree, or "I object" to decline.',
        'If you do not agree, reply “I object” in the chat.',
        "Type 'stop recording' if you want it to stop",
        'Replies may be typed in this chat, or said aloud starting with "LMA": say "LMA, I consent" or "LMA, I object".',
        'Type or say "LMA, I consent" or "LMA, I object".',
        'Say "LMA, I consent" or "LMA, I object".',
        'Type "LMA, I consent" or "LMA, I object" in this chat. An objection made out loud takes effect when Bo records it in LMA.',
    ]) {
        assert.equal(classify(message), null, message);
    }
    // An apostrophe is not a quotation mark.
    assert.equal(classify("Sorry, I don't agree to 'being recorded' today"), 'objection');
    // The phrase on its own, quoted, is still a reply.
    assert.equal(classify('"I object"'), 'objection');
    assert.equal(classify('LMA "I object"'), 'objection');
});

test('an objection phrase outside quotation marks is still found in a message that quotes one', () => {
    assert.equal(classify('Bo said "stop recording" and I agree, stop recording'), 'objection');
    assert.equal(classify('LMA, please stop recording, as in "do not record"'), 'objection');
    // Only the quoted text names the phrase here, so nothing is left to find.
    assert.equal(classify('LMA, "stop recording" please'), null);
});

test('the shipped notices and help lines, posted by someone other than this VP, are not replies', () => {
    const values = { lmaUser: 'Bo', meeting: 'Sync', consentPhrase: 'I consent', objectionPhrase: 'I object' };
    for (const template of [DEFAULT_CONSENT_SETTINGS.notifyNoticeText, DEFAULT_CONSENT_SETTINGS.optInNoticeText]) {
        for (const message of noticeChatMessages(renderNotice(template, values))) {
            assert.equal(classify(message), null, message);
        }
    }
    for (const line of [consentHelpLineFor('chat'), consentHelpLineFor('chat_and_voice'), consentHelpLineFor('voice')]) {
        assert.equal(classify(line), null, line);
    }
});

test('a long notice quoting the phrases is not an objection', () => {
    const notice = `${'Ana is using a meeting assistant to take notes on this call, '.repeat(3)}if you do not agree, reply "I object".`;
    assert.ok(notice.length > 200);
    assert.equal(classify(notice), null);
});

test('a message addressed to LMA that contains an objection phrase is an objection', () => {
    assert.equal(classify('LMA, please stop recording now'), 'objection');
    assert.equal(classify('@LMA I object to being recorded'), 'objection');
});

test('a question for the meeting assistant is not an objection, whatever it mentions', () => {
    assert.equal(classify('LMA ask what should I say if a client asks us to stop recording?'), null);
    assert.equal(classify('@LMA ask: do I object to the minutes?'), null);
    // An addressed objection that is not an ask question is still one.
    assert.equal(classify('@LMA I object to being recorded'), 'objection');
});

test('a long message addressed to LMA is not searched for an objection phrase', () => {
    const notice = `LMA ${'is here to help with this meeting and '.repeat(6)}if you do not agree, reply "I object".`;
    assert.ok(notice.length > 200);
    assert.equal(classify(notice), null);
});

test('the chat commands are not replies', () => {
    for (const message of ['LMA leave', 'LMA stop', 'LMA pause', 'LMA start', 'LMA help']) {
        assert.equal(classify(message), null, message);
    }
});

test('phrases in other scripts and accented letters are matched', () => {
    assert.equal(classifyChatReply("J'accepte", ["j'accepte"], []), 'consent');
    assert.equal(classifyChatReply('Ich stimme zu!', ['ich stimme zu'], []), 'consent');
    assert.equal(classifyChatReply('同意', ['同意'], []), 'consent');
    // Apostrophes are punctuation like any other, curly or straight.
    assert.equal(normalizeReplyText('Don’t  Record!'), normalizeReplyText("don't record"));
});

test('splitPhrases splits the comma-separated setting and drops empty entries', () => {
    assert.deepEqual(splitPhrases('I consent, , I agree ,I accept'), ['I consent', 'I agree', 'I accept']);
    assert.deepEqual(splitPhrases(['a', ' b ']), ['a', 'b']);
    assert.deepEqual(splitPhrases(undefined), []);
});

test('renderNotice fills the four placeholders and leaves other braces as written', () => {
    const text = renderNotice('{LMA_USER} records {meeting}; reply "{consent_phrase}" or "{objection_phrase}" {other}', {
        lmaUser: 'Ana',
        meeting: 'Weekly',
        consentPhrase: 'I consent',
        objectionPhrase: 'I object',
    });
    assert.equal(text, 'Ana records Weekly; reply "I consent" or "I object" {other}');
    assert.equal(renderNotice('{LMA_USER} / {meeting}', {}), 'The meeting organizer / this meeting');
});

test('the notice is posted as single-line messages, split at sentence ends', () => {
    assert.deepEqual(noticeChatMessages('Line one.\nLine two.'), ['Line one. Line two.']);
    const long = Array.from({ length: 30 }, (_, i) => `Sentence number ${i} is here.`).join(' ');
    const messages = noticeChatMessages(long, 200);
    assert.ok(messages.length > 1);
    for (const message of messages) {
        assert.ok(message.length <= 200, message);
        assert.ok(!message.includes('\n'));
    }
    assert.equal(messages.join(' '), long);
    assert.deepEqual(noticeChatMessages(''), []);
});

test('strictness ranks modes and opt-in rules as the configuration layer does', () => {
    assert.ok(atLeastAsStrict(strictness('opt_in', 'owner_attests'), strictness('notify', null)));
    assert.ok(atLeastAsStrict(strictness('opt_in', 'each_participant'), strictness('opt_in', 'owner_attests')));
    assert.ok(!atLeastAsStrict(strictness('opt_in', 'owner_attests'), strictness('opt_in', 'each_participant')));
    assert.ok(!atLeastAsStrict(strictness('off', null), strictness('notify', null)));
});
