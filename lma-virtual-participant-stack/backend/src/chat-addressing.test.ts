/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which LMA Virtual Participant a chat message is for (chat-addressing.ts) and
 * the handles a display name answers to (vp-handle.ts): a VP alone in a meeting
 * answers the bare "LMA", two VPs each answer only their own name, and the
 * matchers downstream see the message with its address reduced to "LMA".
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { ChatAddressing, resolveChatAddress, routeChatMessage } from './chat-addressing.js';
import { QUESTION_STARTS, parseChatQuestion } from './chat-ask.js';
import { ASSISTANT_CHAT_COMMANDS } from './assistant-toggles.js';
import { END_COMMAND_VERBS, details, matchesChatCommand, matchesEndCommand } from './details.js';
import { classifyChatReply } from './consent-matcher.js';
import { isLmaChatCommand } from './meeting-chat.js';
import {
    RESERVED_HANDLE_WORDS,
    isLmaVpName,
    preferredVpHandle,
    vpHandles,
    withoutVpAddress,
} from './vp-handle.js';
import { routeLogsToConsole } from './logger.js';

routeLogsToConsole();
console.log = () => {};

const BOB = 'LMA (Bob Strahan)';
const ALICE = 'LMA (Alice Wong)';
const EMAIL = 'LMA (strahanr@example.com)';

// ---------------------------------------------------------------- handles

test('a display name answers to its owner part and the first word of it', () => {
    assert.deepEqual(vpHandles(BOB), ['bob strahan', 'bob']);
    assert.deepEqual(vpHandles(EMAIL), ['strahanr example com', 'strahanr']);
    assert.deepEqual(vpHandles('LMA Bob'), ['bob']);
    assert.deepEqual(vpHandles('LMA'), []);
    assert.deepEqual(vpHandles('Acme Notes'), []);
    assert.equal(preferredVpHandle(BOB), 'Bob');
    assert.equal(preferredVpHandle(EMAIL), 'strahanr');
    assert.equal(preferredVpHandle('LMA'), null);
});

test('a handle that is a command or question word is not used', () => {
    assert.deepEqual(vpHandles('LMA (Help Desk)'), ['help desk']);
    assert.deepEqual(vpHandles('LMA (Leave)'), []);
    assert.equal(preferredVpHandle('LMA (Help Desk)'), 'Help Desk');
});

test('the reserved words cover every command word and question word', () => {
    const words = [
        ...END_COMMAND_VERBS,
        details.helpCommand,
        details.pauseCommand,
        details.startCommand,
        'ask',
        ...ASSISTANT_CHAT_COMMANDS.flatMap((entry) => entry.phrases.map((phrase) => phrase.split(' ')[0])),
        ...QUESTION_STARTS,
        'consent',
        'object',
    ];
    for (const word of words) {
        assert.ok(RESERVED_HANDLE_WORDS.has(word.toLowerCase()), `"${word}" could be taken as a handle`);
    }
});

test('an LMA Virtual Participant is told from a person by its first word', () => {
    for (const name of [BOB, 'LMA Alice', '@LMA (Bob)', 'lma']) assert.equal(isLmaVpName(name), true, name);
    for (const name of ['Alma Ruiz', 'Selma', 'Bob (LMA team)', '', null]) assert.equal(isLmaVpName(name), false, String(name));
});

// ---------------------------------------------------------------- addressing

test('this VP is addressed by its display name, typed or mentioned, and the address reads as "LMA"', () => {
    for (const [message, text] of [
        ['@LMA (Bob Strahan) leave', 'LMA leave'],
        ['LMA (Bob Strahan) summarize the meeting', 'LMA summarize the meeting'],
        ['lma (bob strahan), help', 'LMA, help'],
        // Teams renders a mention as the name with no "@", and adjacent mention
        // spans can run together.
        ['LMA(Bob Strahan) pause', 'LMA pause'],
        ['LMA\u00A0(Bob\u200B Strahan) pause', 'LMA pause'],
        ['Goodbye LMA (Bob Strahan)!', 'Goodbye LMA!'],
    ]) {
        assert.deepEqual(resolveChatAddress(message, BOB, [ALICE]), { kind: 'self', text }, message);
    }
});

test('this VP is addressed by a handle after "LMA"', () => {
    for (const [message, text, self] of [
        ['@LMA Bob leave', '@LMA leave', BOB],
        ['LMA bob, what was decided?', 'LMA, what was decided?', BOB],
        ['LMA Bob Strahan help', 'LMA help', BOB],
        ['LMA (Bob) translate French', 'LMA translate French', BOB],
        ['LMA strahanr summarize', 'LMA summarize', EMAIL],
        ['@LMA (strahanr@example.com) leave', 'LMA leave', EMAIL],
        ['LMA strahanr@example.com ask who is next?', 'LMA ask who is next?', EMAIL],
    ]) {
        assert.deepEqual(resolveChatAddress(message, self, []), { kind: 'self', text }, message);
    }
});

test('a message for another VP, by its name, its handle or any other parenthesised name, is not for this one', () => {
    for (const message of ['LMA (Alice Wong) leave', '@LMA Alice help', 'LMA alice what was decided?', 'LMA (Carol) leave']) {
        assert.equal(resolveChatAddress(message, BOB, [ALICE]).kind, 'other', message);
    }
    // Reduced to "LMA" all the same, so it is still known to be a command.
    assert.equal(resolveChatAddress('LMA (Alice Wong) leave', BOB, [ALICE]).text, 'LMA leave');
});

test('the bare "LMA" and a message with no "LMA" are told apart', () => {
    assert.deepEqual(resolveChatAddress('LMA leave', BOB, []), { kind: 'bare', text: 'LMA leave' });
    assert.deepEqual(resolveChatAddress('@LMA what was decided?', BOB, [ALICE]), { kind: 'bare', text: '@LMA what was decided?' });
    // A word after "LMA" that is nobody's handle is the command, not an address.
    assert.equal(resolveChatAddress('LMA Carol leave', BOB, [ALICE]).kind, 'bare');
    assert.deepEqual(resolveChatAddress('I agree', BOB, [ALICE]), { kind: 'none', text: 'I agree' });
    assert.equal(resolveChatAddress('Alma says hi', BOB, []).kind, 'none');
});

// ---------------------------------------------------------------- acting

test('a VP alone in the meeting acts on the bare "LMA", as before', () => {
    for (const message of ['LMA leave', '@LMA help', 'LMA ask what was decided?', 'LMA what was decided?', 'LMA consent']) {
        const route = routeChatMessage(message, 'Carol', BOB, []);
        assert.equal(route.act, true, message);
        assert.equal(route.text, message);
    }
});

test('with two VPs, each acts only on its own name, and neither on the bare "LMA"', () => {
    for (const message of ['LMA leave', '@LMA help', 'LMA what was decided?', 'LMA ask anything', 'LMA consent']) {
        assert.equal(routeChatMessage(message, 'Carol', BOB, [ALICE]).act, false, message);
        assert.equal(routeChatMessage(message, 'Carol', BOB, [ALICE]).reason, 'several-vps');
        assert.equal(routeChatMessage(message, 'Carol', ALICE, [BOB]).act, false, message);
    }
    const toBob = routeChatMessage('LMA Bob leave', 'Carol', BOB, [ALICE]);
    assert.equal(toBob.act, true);
    assert.equal(matchesEndCommand(toBob.text), true, 'the matcher reads it as "LMA leave"');
    const atAlice = routeChatMessage('LMA Bob leave', 'Carol', ALICE, [BOB]);
    assert.equal(atAlice.act, false);
    assert.equal(atAlice.reason, 'other-vp');
});

test('every command, question and addressed consent reply works addressed by name', () => {
    const read = (message: string) => {
        const route = routeChatMessage(message, 'Carol', BOB, [ALICE]);
        assert.equal(route.act, true, message);
        return route.text;
    };
    assert.equal(matchesEndCommand(read('LMA Bob leave')), true);
    assert.equal(matchesEndCommand(read('bye LMA (Bob Strahan)')), true);
    assert.equal(matchesChatCommand(read('@LMA Bob help'), details.helpCommand), true);
    assert.equal(matchesChatCommand(read('LMA Bob pause'), details.pauseCommand), true);
    assert.deepEqual(parseChatQuestion(read('LMA Bob ask what next?'), true), { question: 'what next?', explicit: true });
    assert.deepEqual(parseChatQuestion(read('LMA Bob what next?'), true), { question: 'what next?', explicit: false });
    assert.equal(classifyChatReply(read('LMA Bob consent'), [], []), 'consent');
    assert.equal(classifyChatReply(read('LMA (Bob Strahan) object'), [], []), 'objection');
});

test('a consent reply not addressed to LMA is read by every VP, as before', () => {
    for (const self of [BOB, ALICE]) {
        const route = routeChatMessage('I agree', 'Carol', self, self === BOB ? [ALICE] : [BOB]);
        assert.equal(route.act, true);
        assert.equal(route.kind, 'none');
    }
});

test("another VP's own messages are never acted on", () => {
    const route = routeChatMessage('LMA Bob leave', ALICE, BOB, []);
    assert.equal(route.act, false);
    assert.equal(route.reason, 'from-vp');
});

// ---------------------------------------------------------------- the peers

test('another VP is known from a chat sender, and the bare "LMA" then goes unanswered', () => {
    const addressing = new ChatAddressing(() => BOB, () => ['Live Meeting Assistant has left the room.']);
    assert.equal(addressing.route('LMA help', 'Carol').act, true, 'alone');
    addressing.route('Hello. I am an AI Live Meeting Assistant (LMA).', ALICE);
    assert.deepEqual(addressing.peers(), [ALICE]);
    assert.equal(addressing.route('LMA help', 'Carol').act, false);
    assert.equal(addressing.route('LMA Bob help', 'Carol').act, true);
    // Its own name and a person's name are not another VP.
    addressing.notePeer(BOB);
    addressing.notePeer('Carol');
    addressing.notePeer('LMA');
    assert.deepEqual(addressing.peers(), [ALICE]);
    // A VP that posts its exit message has left.
    addressing.route('Live Meeting Assistant has left the room.', ALICE);
    assert.deepEqual(addressing.peers(), []);
    assert.equal(addressing.route('LMA help', 'Carol').act, true);
});

test('a whole participant list, where there is one, decides who is present', () => {
    const addressing = new ChatAddressing(() => BOB);
    addressing.notePeer(ALICE);
    addressing.noteRoster(['Carol', BOB, 'LMA (Dan)']);
    assert.deepEqual(addressing.peers(), ['LMA (Dan)']);
    addressing.noteRoster(['Carol']);
    assert.deepEqual(addressing.peers(), []);
    addressing.noteRoster(null);
    assert.deepEqual(addressing.peers(), [ALICE], 'back to the names seen when the list cannot be read');
});

test('a command for another VP is still a command, so it is not added to the transcript', () => {
    assert.equal(withoutVpAddress('LMA (Alice Wong) leave'), 'LMA leave');
    assert.equal(isLmaChatCommand('LMA (Alice Wong) leave'), true);
    assert.equal(isLmaChatCommand('@LMA (Alice) help'), true);
    assert.equal(isLmaChatCommand('LMA (Alice) what was decided?'), false, 'a question is added');
});

// ---------------------------------------------------------------- the handlers

test('every platform handler routes chat through the addressing before its commands', () => {
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'google-meet.ts', 'webex.ts', 'chime.ts']) {
        const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        const route = source.indexOf('chatAddressing.route(');
        assert.ok(route >= 0, `${file} does not route chat`);
        const consent = source.indexOf('meetingConsent.handleChat(', route);
        assert.ok(consent > route, `${file} reads consent replies before routing`);
        assert.ok(source.slice(route, consent).includes('if (!route.act)'), `${file} acts on messages for another VP`);
    }
});

test('the introduction and the help tell people the name to address this VP by', () => {
    assert.ok(details.introMessages[0].includes(`"LMA ${preferredVpHandle(details.scribeIdentity) ?? details.scribeIdentity} leave"`));
});
