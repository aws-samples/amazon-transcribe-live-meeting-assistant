/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The VP acts only on meeting chat messages sent to everyone: a consent reply,
 * a chat command, a question for the meeting assistant or `LMA leave` sent
 * privately, or with a recipient the handler could not read, is not acted on
 * and gets no reply. The rule is in `ChatAddressing.route`
 * (chat-addressing.ts), which every platform handler calls before its consent,
 * command and question handling; the handlers themselves drive a real browser
 * page, so how they are wired to it is read from their source, as
 * leave-wiring.test.ts does.
 */

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { ChatAddressing } from './chat-addressing.js';
import { ChatAudience, MeetingChatCapture } from './meeting-chat.js';
import { captureLogs, setLogLevel } from './logger.js';
import { parseZoomChatLabel, zoomChatAudience } from './zoom.js';
import { zoomSdkChatAudience } from './zoom-sdk.js';
import { webexChatAudience } from './webex.js';

const BOB = 'LMA (Bob Strahan)';

/** Messages each of which the VP acts on when it is sent to everyone. */
const ACTIONABLE = [
    'LMA, I consent',
    'I consent',
    'LMA, I object',
    'LMA no consent',
    'LMA leave',
    'LMA pause',
    'LMA start',
    'LMA help',
    'LMA ask what was decided?',
    '@LMA summarize the meeting so far',
];

function route(message: string, audience: ChatAudience | undefined) {
    return new ChatAddressing(() => BOB).route(message, 'Bo Chen', audience);
}

test('a message sent privately is not acted on, whatever it says', () => {
    for (const message of ACTIONABLE) {
        const r = route(message, 'direct');
        assert.equal(r.act, false, message);
        assert.equal(r.reason, 'direct', message);
    }
});

test('a message whose recipient could not be read is not acted on', () => {
    for (const message of ACTIONABLE) {
        const r = route(message, undefined);
        assert.equal(r.act, false, message);
        assert.equal(r.reason, 'recipient-unknown', message);
    }
});

test('a message sent to everyone is still acted on', () => {
    for (const message of ACTIONABLE) assert.equal(route(message, 'everyone').act, true, message);
});

test('the debug line for a message not sent to everyone names neither its text nor its sender', () => {
    setLogLevel('debug');
    const logs = captureLogs();
    try {
        new ChatAddressing(() => BOB).route('LMA leave right now', 'Bo Chen', 'direct');
        const line = logs.messages().find((entry) => entry.includes('not sent to everyone'));
        assert.ok(line, 'a debug line is written');
        assert.match(line, /\(direct\)/);
        assert.doesNotMatch(line, /leave|Bo Chen/);
    } finally {
        logs.restore();
        setLogLevel(undefined);
    }
});

// ---------------------------------------------------------------- each platform's recipient

test('Zoom: a private consent reply, command or question is not acted on; one to everyone is', () => {
    const zoomRoute = (label: string) => {
        const { sender, recipient, body } = parseZoomChatLabel(label);
        return new ChatAddressing(() => BOB).route(body, sender, zoomChatAudience(recipient));
    };
    for (const body of ACTIONABLE) {
        for (const recipient of ['You', 'You (Direct Message)', 'Everyone (Privately)', 'LMA (Bob Strahan)']) {
            assert.equal(zoomRoute(`Bo Chen to ${recipient}: ${body}`).act, false, `${recipient}: ${body}`);
            assert.equal(zoomRoute(`Bo Chen to ${recipient}, 10:42 AM, ${body}`).act, false, `${recipient}: ${body}`);
        }
        assert.equal(zoomRoute(`Bo Chen to Everyone: ${body}`).act, true, body);
        assert.equal(zoomRoute(`Bo Chen to Everyone, 10:42 AM, ${body}`).act, true, body);
        assert.equal(zoomRoute(`Bo Chen to Meeting Group Chat: ${body}`).act, true, body);
    }
});

test('Zoom: a sender whose display name holds a comma is read, and the recipient with it', () => {
    for (const label of ['Strahan, Bob to Everyone: LMA leave', 'Strahan, Bob to Everyone, 10:42 AM, LMA leave']) {
        const { sender, recipient, body } = parseZoomChatLabel(label);
        assert.equal(sender, 'Strahan, Bob', label);
        assert.equal(body, 'LMA leave', label);
        assert.equal(new ChatAddressing(() => BOB).route(body, sender, zoomChatAudience(recipient)).act, true, label);
    }
    const { sender, recipient, body } = parseZoomChatLabel('Strahan, Bob to You (Direct Message): LMA leave');
    assert.equal(sender, 'Strahan, Bob');
    assert.equal(new ChatAddressing(() => BOB).route(body, sender, zoomChatAudience(recipient)).act, false);
});

test('Zoom: a label in another language shows no recipient, and is not acted on', () => {
    const { sender, recipient, body } = parseZoomChatLabel('Bo Chen an Alle: LMA, I consent');
    assert.equal(sender, null);
    assert.equal(zoomChatAudience(recipient), undefined);
    assert.equal(new ChatAddressing(() => BOB).route(body, sender, zoomChatAudience(recipient)).act, false);
});

test('Zoom: a label in another language whose message says "to Everyone:" is not read as sent to everyone', () => {
    const { recipient, body } = parseZoomChatLabel('Bo Chen an Alle, 10.42, bitte, sag es to Everyone: LMA leave');
    assert.equal(zoomChatAudience(recipient), undefined);
    assert.equal(new ChatAddressing(() => BOB).route(body, null, zoomChatAudience(recipient)).act, false);
});

test('Zoom Meeting SDK: a message to one participant is not acted on; one to everyone is', () => {
    const sdkRoute = (message: string, receiverId: string | number | null, receiverName: string | null) =>
        new ChatAddressing(() => BOB).route(message, 'Bo Chen', zoomSdkChatAudience(receiverId, receiverName));
    for (const message of ACTIONABLE) {
        assert.equal(sdkRoute(message, 16778240, 'LMA (Bob Strahan)').act, false, message);
        assert.equal(sdkRoute(message, 16778240, null).act, false, message);
        assert.equal(sdkRoute(message, null, null).act, false, `no receiver: ${message}`);
        assert.equal(sdkRoute(message, 0, 'Everyone').act, true, message);
        assert.equal(sdkRoute(message, 0, null).act, true, message);
    }
});

test('Webex: a message the row marks private is not acted on; an unmarked one is', () => {
    for (const message of ACTIONABLE) {
        assert.equal(new ChatAddressing(() => BOB).route(message, 'Bo Chen', webexChatAudience(true)).act, false, message);
        assert.equal(new ChatAddressing(() => BOB).route(message, 'Bo Chen', webexChatAudience(false)).act, true, message);
        assert.equal(new ChatAddressing(() => BOB).route(message, 'Bo Chen', webexChatAudience(undefined)).act, true, message);
    }
});

// ---------------------------------------------------------------- the capture's debug line

test('the chat capture reports a private command as private, not as a command', () => {
    const capture = new MeetingChatCapture({
        settingOn: () => true,
        redacting: () => false,
        capturing: () => true,
        meetingStarted: () => true,
        timelineSeconds: () => 1,
        send: async () => {},
        ownIdentities: () => [BOB],
        ownName: () => BOB,
    });
    const offer = (id: string, text: string, audience: ChatAudience | undefined) =>
        capture.offer({ platform: 'zoom', id, text, sender: 'Bo Chen', audience });
    assert.equal(offer('1', 'LMA leave', 'direct'), 'direct');
    assert.equal(offer('2', 'LMA, I consent', 'direct'), 'direct');
    assert.equal(offer('3', 'LMA pause', undefined), 'recipient-unknown');
    assert.equal(offer('4', 'LMA leave', 'everyone'), 'command');
});

// ---------------------------------------------------------------- the handlers

const source = (file: string): string => readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');

/** What each handler passes as the recipient: read from the message where the platform has private messages. */
const RECIPIENT: Record<string, RegExp> = {
    'zoom.ts': /chatAddressing\.route\(rawBody, sender, audience\)/,
    'zoom-sdk.ts': /chatAddressing\.route\(rawBody, sender, audience\)/,
    'webex.ts': /chatAddressing\.route\(message, sender, webexChatAudience\(privateMark\)\)/,
    'teams.ts': /chatAddressing\.route\(rawMessage, row\.author \?\? null, 'everyone'\)/,
    'teams-sdk.ts': /chatAddressing\.route\(rawBody, sender, 'everyone'\)/,
    'google-meet.ts': /chatAddressing\.route\(rawMessage, sender, 'everyone'\)/,
    'chime.ts': /chatAddressing\.route\(text, sender === 'Amazon Chime' \? null : sender, 'everyone'\)/,
};

test('every handler routes chat with what it read of the recipient', () => {
    const zoom = source('zoom.ts');
    const read = zoom.indexOf('const audience = zoomChatAudience(recipient);');
    assert.ok(read >= 0 && zoom.indexOf('chatAddressing.route(rawBody, sender, audience)', read) > read);
    assert.match(source('zoom-sdk.ts'), /const audience = zoomSdkChatAudience\(chat\.receiverId, chat\.receiverName\);/);
    for (const [file, call] of Object.entries(RECIPIENT)) assert.match(source(file), call, file);
});

test('every handler stops at a message it does not act on, before any consent reply, command or question', () => {
    for (const file of Object.keys(RECIPIENT)) {
        const body = source(file);
        const at = body.search(RECIPIENT[file]);
        const gate = body.indexOf('if (!route.act)', at);
        assert.ok(gate > at, `${file}: no route.act gate`);
        // The end of the gate's block: its return or continue. The 900 and
        // 3000 characters below are size heuristics for the gate's block and
        // for the handler code before the route call; widen them if a handler
        // grows past them. The behaviour itself is pinned by the route tests
        // above; a failure here is about where the gate sits in a handler.
        const block = body.slice(gate, gate + 900);
        const exit = block.search(/\b(return|continue);/);
        assert.ok(exit >= 0, `${file}: the gate does not stop`);
        assert.doesNotMatch(
            block.slice(0, exit),
            /meetingConsent\.handleChat|startChatAnswer|matchesEndCommand|matchesChatCommand|parseAssistantChatCommand|sendMessages|requestEnd/,
            `${file}: acts on the message inside the gate`,
        );
        const after = body.slice(gate);
        for (const action of ['meetingConsent.handleChat(', 'startChatAnswer(', 'matchesEndCommand(']) {
            assert.ok(after.includes(action), `${file}: ${action} is not after the gate`);
            assert.ok(!body.slice(at - 3000 > 0 ? at - 3000 : 0, at).includes(action), `${file}: ${action} before routing`);
        }
    }
});
