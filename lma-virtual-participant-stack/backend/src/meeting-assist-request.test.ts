/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting-assist Lambda's request for an in-meeting request: a question
 * typed in the meeting chat or a lookup the voice assistant makes. Each names
 * its tool scope, so the meeting assistant answers it as the In-meeting
 * requests setting allows, and the voice assistants build it with the same
 * function the chat does.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { inMeetingRequestPayload } from './meeting-assist-request.js';

const ENV = { VP_CALL_ID: 'call-1', LMA_USER: 'owner@example.com', DYNAMODB_TABLE_NAME: 'EventSourcingTable' };

test('a spoken request to the voice assistant is sent as an in-meeting request', () => {
    const payload = inMeetingRequestPayload('what is the weather?', 'voice_assistant', {
        env: ENV,
        fallbackCallId: 'nova-voice-agent',
    });
    assert.deepEqual(payload, {
        text: 'what is the weather?',
        call_id: 'call-1',
        conversation_history: [],
        userEmail: 'owner@example.com',
        toolScope: 'voice_assistant',
        dynamodb_table_name: 'EventSourcingTable',
        dynamodb_pk: 'c#call-1',
    });
});

test('an in-meeting request carries the LMA user, for an unrestricted setting to act for', () => {
    const opts = { fallbackCallId: 'x', fallbackUser: 'NovaAgent' };
    assert.equal(inMeetingRequestPayload('q', 'voice_assistant', { ...opts, env: ENV }).userEmail, 'owner@example.com');
    assert.equal(inMeetingRequestPayload('q', 'voice_assistant', { ...opts, env: {} }).userEmail, 'NovaAgent');
    assert.equal(inMeetingRequestPayload('q', 'meeting_chat', { fallbackCallId: 'x', env: {} }).userEmail, '');
});

test('an in-meeting request names its meeting, or the fallback when none is set', () => {
    const named = inMeetingRequestPayload('q', 'meeting_chat', { env: { MEETING_NAME: 'Weekly' }, fallbackCallId: 'vp-chat' });
    assert.equal(named.call_id, 'Weekly');
    const unnamed = inMeetingRequestPayload('q', 'meeting_chat', { env: {}, fallbackCallId: 'vp-chat' });
    assert.equal(unnamed.call_id, 'vp-chat');
    assert.equal(unnamed.dynamodb_pk, 'c#vp-chat');
});

test('every voice assistant lookup is built as an in-meeting request from the voice assistant', () => {
    // The two voice assistants call the meeting-assist Lambda from inside their
    // classes; their source must build the request with this function.
    for (const file of ['nova-agent.js', 'elevenlabs-agent.js']) {
        const source = readFileSync(new URL(`./${file}`, import.meta.url), 'utf8');
        assert.match(source, /inMeetingRequestPayload\(query, 'voice_assistant'/, file);
        assert.doesNotMatch(source, /userEmail: username/, file);
    }
});
