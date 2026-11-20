/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Who in the meeting may make a request (inMeetingRequesters), on the Virtual
 * Participant's side: it answers `LMA ask` and starts its voice assistant only
 * when anyone may ask, because it cannot tell the owner from a guest.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { createChatAnswerer } from './chat-ask.js';
import {
    IN_MEETING_REQUESTERS_DEFAULT,
    answersInMeetingRequests,
    inMeetingRequesters,
    requestsNotAnsweredText,
} from './in-meeting-requesters.js';
import { routeLogsToConsole } from './logger.js';
import { applySections } from './stack-settings.js';

routeLogsToConsole();
console.log = () => {};
console.warn = () => {};

test('the default is anyone, as the shipped setting is', () => {
    assert.equal(IN_MEETING_REQUESTERS_DEFAULT, 'anyone');
    assert.equal(inMeetingRequesters({}), 'anyone');
    assert.equal(inMeetingRequesters({ IN_MEETING_REQUESTERS: '' }), 'anyone');
    assert.equal(answersInMeetingRequests({}), true);
});

test('each value is read as itself, and anything else as off', () => {
    assert.equal(inMeetingRequesters({ IN_MEETING_REQUESTERS: 'anyone' }), 'anyone');
    assert.equal(inMeetingRequesters({ IN_MEETING_REQUESTERS: 'owner_only' }), 'owner_only');
    assert.equal(inMeetingRequesters({ IN_MEETING_REQUESTERS: 'off' }), 'off');
    assert.equal(inMeetingRequesters({ IN_MEETING_REQUESTERS: 'owner' }), 'off');
});

test('only anyone lets the Virtual Participant answer requests made in the meeting', () => {
    assert.equal(answersInMeetingRequests({ IN_MEETING_REQUESTERS: 'anyone' }), true);
    assert.equal(answersInMeetingRequests({ IN_MEETING_REQUESTERS: 'owner_only' }), false);
    assert.equal(answersInMeetingRequests({ IN_MEETING_REQUESTERS: 'off' }), false);
});

test('the setting is applied from the Meeting Assistant section', () => {
    const env: Record<string, string | undefined> = {};
    const result = applySections({ meeting_assist: { inMeetingRequesters: 'off' } }, env);
    assert.equal(env.IN_MEETING_REQUESTERS, 'off');
    assert.ok(result.applied.includes('IN_MEETING_REQUESTERS'));
    const untouched: Record<string, string | undefined> = {};
    applySections({ meeting_assist: {} }, untouched);
    assert.equal(untouched.IN_MEETING_REQUESTERS, undefined);
});

for (const value of ['owner_only', 'off']) {
    test(`the voice assistant is not started when the setting is ${value}`, () => {
        const env: Record<string, string | undefined> = { VOICE_ASSISTANT_PROVIDER: 'amazon_nova_sonic' };
        const result = applySections({ meeting_assist: { inMeetingRequesters: value } }, env);
        assert.equal(env.VOICE_ASSISTANT_PROVIDER, 'none');
        assert.ok(result.notes.some((note) => note.includes('voice assistant is not started')));
    });

    test(`a provider chosen on the page is not started either when the setting is ${value}`, () => {
        const env: Record<string, string | undefined> = {};
        applySections(
            {
                virtual_participant: { voiceAssistantProvider: 'elevenlabs' },
                meeting_assist: { inMeetingRequesters: value },
            },
            env,
        );
        assert.equal(env.VOICE_ASSISTANT_PROVIDER, 'none');
    });
}

test('the voice assistant is started as configured when anyone may ask', () => {
    const env: Record<string, string | undefined> = { VOICE_ASSISTANT_PROVIDER: 'amazon_nova_sonic' };
    const result = applySections({ meeting_assist: { inMeetingRequesters: 'anyone' } }, env);
    assert.equal(env.VOICE_ASSISTANT_PROVIDER, 'amazon_nova_sonic');
    assert.deepEqual(result.notes, []);
});

test('with no voice assistant configured nothing is noted', () => {
    const env: Record<string, string | undefined> = { VOICE_ASSISTANT_PROVIDER: 'none' };
    const result = applySections({ meeting_assist: { inMeetingRequesters: 'off' } }, env);
    assert.deepEqual(result.notes, []);
    assert.ok(!result.applied.includes('VOICE_ASSISTANT_PROVIDER'));
});

function answerer(allowed: boolean) {
    const asked: string[] = [];
    const a = createChatAnswerer(
        async (question) => {
            asked.push(question);
            return 'An answer.';
        },
        { available: () => true, paused: () => false, requestsAllowed: () => allowed },
    );
    return { a, asked };
}

test('a question typed in the chat is not sent on when requests are not answered', async () => {
    const { a, asked } = answerer(false);
    const messages = await a.answer({ question: 'what did we decide?', explicit: true }, 'Guest', 'zoom');
    assert.deepEqual(asked, []);
    assert.equal(messages.length, 1);
    assert.match(messages[0], /^Live Meeting Assistant: /);
});

test('an @LMA aside is left alone when requests are not answered', async () => {
    const { a, asked } = answerer(false);
    const messages = await a.answer({ question: 'what did we decide?', explicit: false }, 'Guest', 'zoom');
    assert.deepEqual(asked, []);
    assert.deepEqual(messages, []);
});

test('a question typed in the chat is answered when anyone may ask', async () => {
    const { a, asked } = answerer(true);
    await a.answer({ question: 'what did we decide?', explicit: true }, 'Guest', 'zoom');
    assert.deepEqual(asked, ['what did we decide?']);
});

test('the notice says why a question is not answered', () => {
    assert.match(requestsNotAnsweredText({ IN_MEETING_REQUESTERS: 'off' }), /turned off/);
    assert.match(requestsNotAnsweredText({ IN_MEETING_REQUESTERS: 'owner_only' }), /owner/);
});
