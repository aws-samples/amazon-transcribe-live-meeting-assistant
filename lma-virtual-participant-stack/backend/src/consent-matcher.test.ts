/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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
import { DEFAULT_CONSENT_SETTINGS } from './meeting-consent.js';

const CONSENT = DEFAULT_CONSENT_SETTINGS.consentPhrases;
const OBJECTION = DEFAULT_CONSENT_SETTINGS.objectionPhrases;
const classify = (message: string) => classifyChatReply(message, CONSENT, OBJECTION);
const classifySpoken = (utterance: string) => classifySpokenReply(utterance, CONSENT, OBJECTION);

test('a spoken reply is read as a chat message is: the whole utterance must be the reply', () => {
    assert.equal(classifySpoken('I consent.'), 'consent');
    assert.equal(classifySpoken('Stop recording!'), 'objection');
    assert.equal(classifySpoken('I do not consent.'), 'objection', 'objections first');
    assert.equal(classifySpoken('I agree with what Bo said about the roadmap.'), null);
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

test('a sentence that merely contains a phrase is ordinary chat', () => {
    for (const message of [
        "I agree with Bo's point",
        "I don't agree with the roadmap",
        'please do not record the demo twice',
        'I consent to the budget',
        'LMA consent is a good feature',
        'Reply "I consent" to agree, or "I object" to decline.',
        '',
        '   ',
    ]) {
        assert.equal(classify(message), null, message);
    }
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
