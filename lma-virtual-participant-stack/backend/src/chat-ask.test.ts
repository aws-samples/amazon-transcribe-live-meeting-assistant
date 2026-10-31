/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * `LMA ask <question>` / `@LMA <question>`: questions typed in the meeting chat,
 * answered in the chat by the meeting assistant.
 *
 * The agent call is injected, so these run with no Lambda. What matters: the
 * other chat commands keep winning, an answer reaches the chat as plain
 * single-line text within the platform's limit, one sender cannot flood the
 * agent, and the Virtual Participant never answers itself.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    ANSWER_PREFIX,
    CHAT_MESSAGE_LIMITS,
    MAX_ANSWER_MESSAGES,
    createChatAnswerer,
    isOwnChatSender,
    parseChatQuestion,
    splitForChat,
    startChatAnswer,
    type ChatPlatform,
} from './chat-ask.js';
import { details } from './details.js';

console.log = () => {};
console.warn = () => {};

// ---------------------------------------------------------------------------
// Recognising a question
// ---------------------------------------------------------------------------

test('LMA ask and @LMA introduce a question', () => {
    assert.deepEqual(parseChatQuestion('LMA ask what did we decide?'), { question: 'what did we decide?', explicit: true });
    assert.deepEqual(parseChatQuestion('@LMA ask: who owns the budget'), { question: 'who owns the budget', explicit: true });
    assert.deepEqual(parseChatQuestion('lma, ask, summarise the last ten minutes'), {
        question: 'summarise the last ten minutes',
        explicit: true,
    });
    assert.deepEqual(parseChatQuestion('@LMA what are the action items?', true), {
        question: 'what are the action items?',
        explicit: false,
    });
});

test('the other chat commands win over @LMA', () => {
    for (const message of ['@LMA help', '@LMA leave', '@LMA pause', '@LMA start', '@LMA translate', '@LMA listen', '@LMA translate French']) {
        assert.equal(parseChatQuestion(message, true), null, `"${message}" was taken as a question`);
    }
});

test('LMA without @ or "ask" is not a question, and nor is a mention mid-sentence', () => {
    for (const message of ['LMA what are the action items', 'thanks LMA', 'I asked @LMA earlier', 'LMA asked me', '@LMA', 'LMA ask']) {
        const parsed = parseChatQuestion(message, true);
        if (message === 'LMA ask') {
            assert.deepEqual(parsed, { question: '', explicit: true }, 'a bare "LMA ask" is answered with how to use it');
        } else {
            assert.equal(parsed, null, `"${message}"`);
        }
    }
});

test("no message the Virtual Participant posts is a question", () => {
    for (const platform of Object.keys(CHAT_MESSAGE_LIMITS) as ChatPlatform[]) {
        for (const message of splitForChat('@LMA ask LMA ask something. '.repeat(80), CHAT_MESSAGE_LIMITS[platform])) {
            assert.ok(message.startsWith(ANSWER_PREFIX));
            assert.equal(parseChatQuestion(message, true), null, `own answer parsed as a question: "${message.slice(0, 60)}"`);
        }
    }
});

test("the Virtual Participant's own configured messages are never questions, whatever they say", () => {
    const before = details.introMessages;
    details.introMessages = ['@LMA is here: type LMA help for the commands'];
    try {
        assert.equal(parseChatQuestion(details.introMessages[0], true), null);
    } finally {
        details.introMessages = before;
    }
    assert.ok(parseChatQuestion('@LMA is here: what next?', true), 'only the exact configured text is excluded');
});

test("the Virtual Participant's own name is recognised as a sender", () => {
    assert.equal(isOwnChatSender(details.scribeIdentity), true);
    assert.equal(isOwnChatSender('You'), true);
    assert.equal(isOwnChatSender('Alice'), false);
    assert.equal(isOwnChatSender(null), false);
});

// ---------------------------------------------------------------------------
// Fitting an answer into the chat
// ---------------------------------------------------------------------------

test('a short answer is one prefixed message', () => {
    assert.deepEqual(splitForChat('The budget is approved.', 500), ['Live Meeting Assistant: The budget is approved.']);
});

test('a long answer is split within the limit, numbered, and cut after the last message', () => {
    for (const platform of Object.keys(CHAT_MESSAGE_LIMITS) as ChatPlatform[]) {
        const limit = CHAT_MESSAGE_LIMITS[platform];
        const messages = splitForChat('This is one sentence of the answer. '.repeat(200), limit);
        assert.equal(messages.length, MAX_ANSWER_MESSAGES, platform);
        messages.forEach((message, i) => {
            assert.ok(message.length <= limit, `${platform} message ${i + 1} is ${message.length} > ${limit}`);
            assert.ok(message.startsWith(`${ANSWER_PREFIX} (${i + 1}/${MAX_ANSWER_MESSAGES}): `));
            assert.doesNotMatch(message, /[\r\n]/);
        });
        assert.ok(messages[messages.length - 1].endsWith('…'));
    }
});

test('an answer split in two breaks at a sentence end', () => {
    const text = `${'a'.repeat(300)}. ${'b'.repeat(300)}.`;
    const messages = splitForChat(text, 500);
    assert.equal(messages.length, 2);
    assert.ok(messages[0].endsWith(`${'a'.repeat(300)}.`));
    assert.ok(messages[1].endsWith(`${'b'.repeat(300)}.`));
});

// ---------------------------------------------------------------------------
// Answering
// ---------------------------------------------------------------------------

function answerer(reply: string | Error = 'An answer.', start = 0) {
    let clock = start;
    const asked: string[] = [];
    const a = createChatAnswerer(
        async (question) => {
            asked.push(question);
            if (reply instanceof Error) throw reply;
            return reply;
        },
        { intervalMs: 20_000, now: () => clock, available: () => true, paused: () => false },
    );
    return { a, asked, advance: (ms: number) => (clock += ms) };
}

const Q = (question: string, explicit = true) => ({ question, explicit });

test('a markdown answer is posted as plain single-line text', async () => {
    const { a, asked } = answerer('## Action items\n\n- **Alice**: send the [deck](https://example.com/deck)\n- Bob: book the room');
    const messages = await a.answer(Q('what are the action items?'), 'Carol', 'zoom');
    assert.deepEqual(asked, ['what are the action items?']);
    assert.equal(messages.length, 1);
    assert.doesNotMatch(messages[0], /[*#\[\]\n]/);
    assert.match(
        messages[0],
        /^Live Meeting Assistant: Action items\. Alice: send the deck \(https:\/\/example\.com\/deck\)\. Bob: book the room$/,
    );
});

test('a chat answer keeps full link addresses, which a reader can follow', async () => {
    const { a } = answerer('Details at https://docs.example.com/guide/setup#step-2 and <https://example.org/a>.');
    const [message] = await a.answer(Q('where are the docs?'), 'Carol', 'zoom');
    assert.equal(
        message,
        'Live Meeting Assistant: Details at https://docs.example.com/guide/setup#step-2 and https://example.org/a.',
    );
});

test('each sender may ask once per interval, and is told so once', async () => {
    const { a, asked, advance } = answerer();
    assert.equal((await a.answer(Q('one'), 'Alice', 'zoom')).length, 1);
    const wait = await a.answer(Q('two'), 'Alice', 'zoom');
    assert.match(wait[0], /one question every 20 seconds/);
    assert.deepEqual(await a.answer(Q('three'), 'Alice', 'zoom'), [], 'told only once per window');
    assert.equal((await a.answer(Q('from bob'), 'Bob', 'zoom')).length, 1, "another sender's question is answered");
    advance(20_000);
    assert.equal((await a.answer(Q('four'), 'Alice', 'zoom')).length, 1);
    assert.deepEqual(asked, ['one', 'from bob', 'four']);
});

test('one question is answered at a time', async () => {
    let release: (value: string) => void = () => {};
    const a = createChatAnswerer(() => new Promise<string>((resolve) => (release = resolve)), {
        available: () => true,
        paused: () => false,
    });
    const first = a.answer(Q('slow'), 'Alice', 'teams-sdk');
    const second = await a.answer(Q('meanwhile'), 'Bob', 'teams-sdk');
    assert.match(second[0], /still answering the previous question/);
    assert.deepEqual(await a.answer(Q('again'), 'Bob', 'teams-sdk'), [], 'told once while busy');
    release('Done.');
    assert.deepEqual(await first, ['Live Meeting Assistant: Done.']);
});

test("the Virtual Participant's own messages are never answered", async () => {
    const { a, asked } = answerer();
    assert.deepEqual(await a.answer(Q('anything'), details.scribeIdentity, 'webex'), []);
    assert.deepEqual(asked, []);
});

test('a failed agent call is answered with an apology, not silence or an error', async () => {
    const { a } = answerer(new Error('Lambda timed out'));
    const messages = await a.answer(Q('anything'), 'Alice', 'chime');
    assert.match(messages[0], /could not get an answer/);
});

test('an agent that never answers frees the slot after the timeout', async () => {
    let calls = 0;
    const a = createChatAnswerer(
        () => (++calls === 1 ? new Promise<string>(() => {}) : Promise.resolve('Fine.')),
        { available: () => true, timeoutMs: 20, paused: () => false },
    );
    assert.match((await a.answer(Q('slow'), 'Alice', 'zoom'))[0], /could not get an answer/);
    assert.deepEqual(await a.answer(Q('next'), 'Bob', 'zoom'), ['Live Meeting Assistant: Fine.']);
});

test('without a meeting assistant an @LMA aside is not a question, but LMA ask still is', () => {
    assert.equal(parseChatQuestion('@LMA what are the action items?', false), null);
    assert.deepEqual(parseChatQuestion('LMA ask what are the action items?', false), {
        question: 'what are the action items?',
        explicit: true,
    });
});

test('while the transcript is paused, questions are not sent and only LMA ask is told why', async () => {
    const asked: string[] = [];
    const a = createChatAnswerer(async (q) => (asked.push(q), 'unused'), { available: () => true, paused: () => true });
    assert.match((await a.answer(Q('anything', true), 'Alice', 'zoom'))[0], /paused; ask again after someone types "LMA start"/);
    assert.deepEqual(await a.answer(Q('anything', false), 'Bob', 'zoom'), []);
    assert.deepEqual(asked, []);
});

test('with no Strands meeting assistant, only an explicit LMA ask is told', async () => {
    const a = createChatAnswerer(async () => 'unused', { available: () => false });
    assert.match((await a.answer(Q('anything', true), 'Alice', 'zoom'))[0], /Strands meeting assistant/);
    assert.deepEqual(await a.answer(Q('anything', false), 'Alice', 'zoom'), []);
});

test('an empty or overlong question is answered without asking the agent', async () => {
    const { a, asked } = answerer();
    assert.match((await a.answer(Q(''), 'Alice', 'zoom'))[0], /type your question after "LMA ask"/);
    assert.match((await a.answer(Q('x'.repeat(501)), 'Bob', 'zoom'))[0], /under 500 characters/);
    assert.deepEqual(asked, []);
});

test('startChatAnswer posts the answer through the platform send and never throws', async () => {
    const { a } = answerer('Posted.');
    const sent: string[][] = [];
    await startChatAnswer(Q('q'), 'Alice', 'zoom', async (m) => void sent.push(m), a);
    assert.deepEqual(sent, [['Live Meeting Assistant: Posted.']]);

    const { a: b } = answerer('Posted.');
    await startChatAnswer(Q('q'), 'Alice', 'zoom', async () => { throw new Error('page closed'); }, b);
});
