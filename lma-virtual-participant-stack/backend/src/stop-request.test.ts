/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Recognising "stop talking" in a meeting transcript.
 *
 * Two invariants matter equally here. A participant who asks the voice
 * assistant to be quiet must be understood however they phrase it, and an
 * ordinary follow-up question that happens to contain the word "stop" must
 * leave the assistant talking — a false positive silences it until it is next
 * addressed by wake phrase, which is worse than the model being slightly slow
 * to notice.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { configuredWakePhrases, isStopTalkingRequest, DEFAULT_WAKE_PHRASES } from './stop-request.js';

const WAKE_PHRASES = ['hey alex', 'ok alex', 'hi alex', 'hello alex'];

test('a request for silence addressed to the assistant is recognised', () => {
    const requests = [
        'Hey Alex, stop.',
        'Hey Alex, stop talking',
        'Hey Alex, stop talking!',
        'hey alex stop speaking',
        'Hey Alex stop it',
        'OK Alex, be quiet',
        'Hi Alex, quiet please',
        'hello alex shut up',
        'Hey Alex, mute yourself',
        'Hey Alex, mute',
        'Hey Alex, silence',
        'Hey Alex, can you please stop talking now?',
        'Hey Alex, would you stop',
        "Hey Alex, that's enough",
        'Hey Alex, that is enough',
        'Hey Alex, enough',
        'HEY ALEX, STOP TALKING',
    ];
    for (const request of requests) {
        assert.equal(isStopTalkingRequest(request, WAKE_PHRASES), true, `not recognised: "${request}"`);
    }
});

test('an utterance carrying any other request is left alone', () => {
    // Each of these would silence the assistant mid-answer if the match were
    // keyword-based rather than whole-utterance.
    const notRequests = [
        'Hey Alex, why did the build stop?',
        'Hey Alex, can you stop the recording and summarise the last hour?',
        'Hey Alex, when did the deployment stop working',
        'Hey Alex, tell Bob to be quiet',
        'Hey Alex, what should we do about the quiet period?',
        'Hey Alex, do not stop',
        "Hey Alex, don't stop",
        'Hey Alex, is that enough detail?',
        'Hey Alex, enough about the budget, what about hiring',
        'Hey Alex, mute the other participants',
        'Hey Alex, stop talking about the roadmap and cover the metrics',
    ];
    for (const utterance of notRequests) {
        assert.equal(isStopTalkingRequest(utterance, WAKE_PHRASES), false, `wrongly recognised: "${utterance}"`);
    }
});

test('a stop request must be addressed to the assistant', () => {
    // Two people in a meeting telling each other to be quiet must not silence
    // the assistant.
    for (const utterance of ['stop talking', 'be quiet', 'Bob, shut up', 'please stop', 'mute yourself']) {
        assert.equal(isStopTalkingRequest(utterance, WAKE_PHRASES), false, `wrongly recognised: "${utterance}"`);
    }
});

test('the wake phrase alone is not a stop request', () => {
    // This is the plain "address the assistant" case and must still activate it.
    for (const utterance of ['Hey Alex', 'Hey Alex?', 'hey alex are you there', '']) {
        assert.equal(isStopTalkingRequest(utterance, WAKE_PHRASES), false, `wrongly recognised: "${utterance}"`);
    }
});

test('the deployment\'s own wake phrases are what count', () => {
    // A deployment that renamed the assistant must get the same behaviour, and
    // must not respond to the default name.
    assert.equal(isStopTalkingRequest('hey assistant stop talking', ['hey assistant']), true);
    assert.equal(isStopTalkingRequest('hey alex stop talking', ['hey assistant']), false);
});

test('configured wake phrases are read from the environment, trimmed and lowercased', () => {
    assert.deepEqual(configuredWakePhrases({ VOICE_ASSISTANT_WAKE_PHRASES: ' Hey Assistant , OK Bot ' }), [
        'hey assistant',
        'ok bot',
    ]);
});

test('an unset or empty wake-phrase variable falls back to the defaults', () => {
    const expected = DEFAULT_WAKE_PHRASES.split(',');
    assert.deepEqual(configuredWakePhrases({}), expected);
    assert.deepEqual(configuredWakePhrases({ VOICE_ASSISTANT_WAKE_PHRASES: '' }), expected);
});
