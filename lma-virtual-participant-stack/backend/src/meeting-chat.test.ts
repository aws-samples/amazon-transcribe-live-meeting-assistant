/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting chat capture (meeting-chat.ts): which messages become transcript
 * segments, and what those segments look like.
 *
 * What is NOT covered here: what each platform's chat panel actually hands the
 * node side in a live meeting (sender, id, recipient), which the capture's
 * debug line exists to measure. The platforms' own parsing that runs node-side
 * (the Zoom label, the Zoom SDK recipient, the Meet group header) is tested
 * below and in google-meet-page.test.ts.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, it, test } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { END_COMMAND_VERBS, details } from './details.js';
import { ASSISTANT_CHAT_COMMANDS } from './assistant-toggles.js';
import { MEETING_CHAT_CHANNEL } from './channels.js';
import {
    ANSWER_WAIT_MS,
    CAPTURE_MEETING_CHAT_DEFAULT,
    ChatOutcome,
    ID_LESS_DUPLICATE_MS,
    IncomingChat,
    MAX_CHAT_TEXT_LENGTH,
    MAX_PER_MEETING,
    MAX_PER_MINUTE,
    MeetingChatCapture,
    MeetingChatDeps,
    OWN_MESSAGE_TTL_MS,
    OwnChatLedger,
    UNKNOWN_CHAT_SENDER,
    captureMeetingChatSetting,
    chatTranscript,
    contentRedactionRequested,
    isLmaChatCommand,
    noteSentChat,
    ownChatLedger,
} from './meeting-chat.js';
import type { TranscriptSegment } from './kinesis-stream.js';
import { kinesisStreamManager } from './kinesis-stream.js';
import { captureLogs, setLogLevel } from './logger.js';
import { parseZoomChatLabel, zoomChatAudience } from './zoom.js';
import { zoomSdkChatAudience } from './zoom-sdk.js';

// ---------------------------------------------------------------------------
// A capture with every gate open, a fake clock, and the segments it writes
// ---------------------------------------------------------------------------

interface Harness {
    capture: MeetingChatCapture;
    sent: TranscriptSegment[];
    state: { settingOn: boolean; redacting: boolean; capturing: boolean; started: boolean; timeline: number; now: number };
    ledger: OwnChatLedger;
    offer: (chat: Partial<IncomingChat>) => ChatOutcome;
}

function harness(overrides: Partial<MeetingChatDeps> = {}): Harness {
    const state = { settingOn: true, redacting: false, capturing: true, started: true, timeline: 12.5, now: 1_000_000 };
    const sent: TranscriptSegment[] = [];
    const ledger = new OwnChatLedger(() => state.now);
    const capture = new MeetingChatCapture({
        settingOn: () => state.settingOn,
        redacting: () => state.redacting,
        capturing: () => state.capturing,
        meetingStarted: () => state.started,
        timelineSeconds: () => state.timeline,
        send: async (segment) => {
            sent.push(segment);
        },
        ownIdentities: () => ['LMA (Alice)', 'LMA'],
        ownName: () => 'LMA (Alice)',
        ledger,
        now: () => state.now,
        ...overrides,
    });
    const offer = (chat: Partial<IncomingChat>) =>
        capture.offer({ platform: 'zoom', text: 'hello', sender: 'Bo Chen', audience: 'everyone', ...chat } as IncomingChat);
    return { capture, sent, state, ledger, offer };
}

// ---------------------------------------------------------------------------
// The commands stay out, whatever state the meeting is in
// ---------------------------------------------------------------------------

test('every LMA chat command is a command, in either word order and any case', () => {
    const commands = [
        ...END_COMMAND_VERBS.flatMap((verb) => [`LMA ${verb}`, `${verb} LMA`, `@lma ${verb.toUpperCase()}!`]),
        ...[details.helpCommand, details.pauseCommand, details.startCommand].flatMap((c) => [
            `LMA ${c.toLowerCase()}`,
            `@LMA, ${c}?`,
            `${c.toLowerCase()} lma`,
        ]),
        ...ASSISTANT_CHAT_COMMANDS.flatMap((entry) => entry.phrases.map((phrase) => `LMA ${phrase}`)),
        'LMA translate to Spanish',
        'LMA consent',
        'LMA no consent',
        'LMA object',
        '@LMA consent.',
        'LMA ask',
        'LMA ask   ',
    ];
    for (const command of commands) {
        assert.equal(isLmaChatCommand(command), true, `not treated as a command: "${command}"`);
    }
});

test('a question for the meeting assistant is not a command, so it is added', () => {
    for (const question of ['LMA ask what did we decide about the budget?', '@LMA what is the next step?', 'lma, ask: who owns this?']) {
        assert.equal(isLmaChatCommand(question), false, question);
    }
});

test('ordinary chat, including prose about LMA or a command, is not a command', () => {
    for (const message of [
        'I have to leave at 3',
        'Type "LMA pause" to pause the transcript',
        'LMA paused for lunch?',
        'pause',
        'consent',
        'Is LMA recording this?',
        'https://example.com/doc',
        '',
    ]) {
        assert.equal(isLmaChatCommand(message), false, message);
    }
});

test('"LMA start" typed while the meeting is running is a command and is not added', () => {
    const h = harness();
    assert.equal(details.start, true);
    assert.equal(h.offer({ text: 'LMA start' }), 'command');
    assert.equal(h.offer({ text: 'LMA pause', id: 'p1' }), 'command');
    assert.equal(h.sent.length, 0);
});

test('a question for the assistant is added under its sender', () => {
    const h = harness();
    assert.equal(h.offer({ text: 'LMA ask what did we decide?', sender: 'Bo Chen' }), 'captured');
    assert.equal(h.sent[0].transcript, 'LMA ask what did we decide?');
    assert.equal(h.sent[0].speaker, 'Bo Chen');
});

test('a bare "LMA <question>?" is a question, not a command, so it is added', () => {
    assert.equal(isLmaChatCommand('LMA what was just discussed?'), false);
    const h = harness();
    assert.equal(h.offer({ text: 'LMA what was just discussed?', id: 'q1' }), 'captured');
});

// ---------------------------------------------------------------------------
// The VP's answer to a question
// ---------------------------------------------------------------------------

const ANSWER = ['Live Meeting Assistant: The team agreed to ship on Friday.'];

test("the VP's answer to a captured question is added once, under the VP's display name", () => {
    const h = harness();
    const question = { platform: 'zoom' as const, text: 'LMA what was just discussed?', sender: 'Bo Chen', id: 'q1' };
    assert.equal(h.offer({ ...question }), 'captured');
    assert.equal(h.capture.offerAnswer(question, ANSWER), 1);
    assert.equal(h.sent.length, 2);
    const answer = h.sent[1];
    assert.equal(answer.channel, MEETING_CHAT_CHANNEL);
    assert.equal(answer.speaker, 'LMA (Alice)');
    assert.equal(answer.transcript, ANSWER[0]);
    assert.equal(answer.isPartial, false);
    assert.match(answer.segmentId, /^chat-zoom-/);
    assert.notEqual(answer.segmentId, h.sent[0].segmentId);
    assert.equal(h.capture.offerAnswer(question, ANSWER), 0, 'one answer per question');
    assert.equal(h.sent.length, 2);
});

test('the answer read back from the chat panel is the VP\'s own message and is not added again', () => {
    const h = harness();
    const question = { platform: 'teams' as const, text: 'LMA ask who owns the budget?', sender: null, id: null };
    assert.equal(h.offer({ ...question }), 'captured');
    // What every handler's sendMessages does before posting.
    h.ledger.record(ANSWER);
    assert.equal(h.capture.offerAnswer(question, ANSWER), 1);
    assert.equal(h.offer({ platform: 'teams', text: ANSWER[0], sender: null, id: 'a1' }), 'own');
    assert.equal(h.offer({ platform: 'teams', text: ANSWER[0], sender: 'LMA (Alice)', id: 'a2' }), 'own');
    assert.equal(h.sent.length, 2);
});

test('an answer is not added when its question was not: private, before the meeting, or with the capture off', () => {
    const h = harness();
    const direct = { platform: 'zoom' as const, text: 'LMA ask anything?', sender: 'Bo Chen', id: 'd1' };
    assert.equal(h.offer({ ...direct, audience: 'direct' }), 'direct');
    assert.equal(h.capture.offerAnswer(direct, ANSWER), 0);

    h.state.settingOn = false;
    const off = { platform: 'zoom' as const, text: 'LMA ask anything?', sender: 'Bo Chen', id: 'o1' };
    assert.equal(h.offer({ ...off }), 'disabled');
    assert.equal(h.capture.offerAnswer(off, ANSWER), 0);
    h.state.settingOn = true;

    // An answer to a question this capture never saw.
    assert.equal(h.capture.offerAnswer({ platform: 'zoom', text: 'LMA ask x?', sender: 'Bo Chen', id: 'never' }, ANSWER), 0);
    // A captured message that is not a question.
    assert.equal(h.offer({ text: 'hello all', id: 'h1' }), 'captured');
    assert.equal(h.capture.offerAnswer({ platform: 'zoom', text: 'hello all', sender: 'Bo Chen', id: 'h1' }, ANSWER), 0);
    assert.equal(h.sent.length, 1);
});

test('an answer is not added when the transcript was paused while the agent answered, or too late', () => {
    const h = harness();
    const first = { platform: 'zoom' as const, text: 'LMA ask one?', sender: 'Bo Chen', id: 'p1' };
    assert.equal(h.offer({ ...first }), 'captured');
    h.state.capturing = false;
    assert.equal(h.capture.offerAnswer(first, ANSWER), 0);
    h.state.capturing = true;

    const second = { platform: 'zoom' as const, text: 'LMA ask two?', sender: 'Bo Chen', id: 'p2' };
    assert.equal(h.offer({ ...second }), 'captured');
    h.state.now += ANSWER_WAIT_MS;
    assert.equal(h.capture.offerAnswer(second, ANSWER), 0);
    assert.equal(h.sent.length, 2);
});

test('every handler that answers chat questions offers the answer to the capture', () => {
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'google-meet.ts']) {
        const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        const call = source.indexOf('void startChatAnswer(');
        assert.ok(call >= 0, `${file} does not answer chat questions`);
        assert.ok(source.slice(call, call + 600).includes('meetingChat.offerAnswer('), `${file} does not add the answer`);
    }
});

// ---------------------------------------------------------------------------
// The VP's own messages
// ---------------------------------------------------------------------------

describe('the own-message ledger', () => {
    it('recognises a posted message read back, ignoring case, punctuation and spacing', () => {
        let now = 0;
        const ledger = new OwnChatLedger(() => now);
        ledger.record(['Live Meeting Assistant started.']);
        assert.equal(ledger.matches('live meeting assistant STARTED'), true);
        assert.equal(ledger.matches('Live Meeting Assistant stopped.'), false);
        now = OWN_MESSAGE_TTL_MS;
        assert.equal(ledger.matches('Live Meeting Assistant started.'), false, 'kept past its time');
    });

    it('matches a long message the panel shows with something added, but not a short one inside another', () => {
        const ledger = new OwnChatLedger(() => 0);
        ledger.record(['Live Meeting Assistant: the budget was approved on Tuesday.', 'OK']);
        assert.equal(ledger.matches('Live Meeting Assistant: the budget was approved on Tuesday. (edited)'), true);
        assert.equal(ledger.matches('OK, see you then'), false);
        assert.equal(ledger.matches('ok'), true);
    });

    it('keeps text in any script', () => {
        const ledger = new OwnChatLedger(() => 0);
        ledger.record(['Live Meeting Assistant が開始しました']);
        assert.equal(ledger.matches('Live Meeting Assistant が開始しました'), true);
        assert.equal(ledger.matches('別のメッセージ'), false);
    });
});

test('a message the VP posted is not added when it is read back, though the platform shows no sender', () => {
    const h = harness();
    h.ledger.record(['Live Meeting Assistant (1/2): the budget was approved on Tuesday.']);
    assert.equal(h.offer({ text: 'Live Meeting Assistant (1/2): the budget was approved on Tuesday.', sender: null }), 'own');
    assert.equal(h.sent.length, 0);
});

test('noteSentChat feeds the ledger the capture reads by default', () => {
    const capture = new MeetingChatCapture({
        settingOn: () => true,
        redacting: () => false,
        capturing: () => true,
        meetingStarted: () => true,
        timelineSeconds: () => 1,
        send: async () => undefined,
        ownIdentities: () => [],
        ownName: () => 'LMA',
    });
    noteSentChat(['Thanks Bo — I will head out now.']);
    assert.equal(ownChatLedger.matches('Thanks Bo — I will head out now.'), true);
    assert.equal(capture.offer({ platform: 'teams', text: 'Thanks Bo — I will head out now.', sender: null }), 'own');
});

test("a message under the VP's own name, or shown as You, is not added", () => {
    const h = harness();
    assert.equal(h.offer({ sender: 'LMA (Alice)', id: 'a' }), 'own');
    assert.equal(h.offer({ sender: 'You', id: 'b' }), 'own');
    assert.equal(h.offer({ sender: details.scribeName, id: 'c' }), 'own');
    assert.equal(h.offer({ sender: 'Alice', id: 'd' }), 'captured');
});

test('every handler that adds chat records what it posts, and no handler keeps or logs chat any more', () => {
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'google-meet.ts']) {
        const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        const send = source.indexOf('private async sendMessages(');
        assert.ok(send >= 0, `${file} has no sendMessages`);
        const body = source.slice(send, source.indexOf('\n    }\n', send));
        assert.ok(body.includes('noteSentChat(messages);'), `${file}'s sendMessages does not record what it posts`);
        assert.ok(source.includes('meetingChat.offer({'), `${file} does not offer chat to the capture`);
    }
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'webex.ts', 'google-meet.ts', 'chime.ts', 'details.ts']) {
        const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        assert.ok(!source.includes('details.messages'), `${file} still keeps chat in details.messages`);
        assert.ok(!source.includes('details.attachments'), `${file} still keeps chat attachments`);
        assert.ok(!source.includes("'New message:'"), `${file} still logs chat text`);
    }
    // Amazon Chime is left out of the capture, and Webex is only observed.
    assert.ok(!readFileSync(new URL('../src/chime.ts', import.meta.url), 'utf8').includes('meetingChat'));
    const webex = readFileSync(new URL('../src/webex.ts', import.meta.url), 'utf8');
    assert.ok(!webex.includes('meetingChat.offer('), 'Webex chat must not be added to the transcript');
    assert.ok(webex.includes('meetingChat.observe({'), 'Webex chat is no longer observed for the diagnostics');
    // No handler logs the text it posts.
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'webex.ts', 'google-meet.ts', 'chime.ts']) {
        const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        assert.ok(!/log\.\w+\('Sent messages:', messages\)/.test(source), `${file} logs the messages it posts`);
    }
});

// ---------------------------------------------------------------------------
// Private messages, duplicates and the panel's backlog
// ---------------------------------------------------------------------------

test('only a message sent to everyone is added: a private one, or one whose recipient was not read, is not', () => {
    const h = harness();
    assert.equal(h.offer({ id: '1', audience: 'direct' }), 'direct');
    assert.equal(h.offer({ id: '2', audience: 'everyone' }), 'captured');
    assert.equal(h.offer({ id: '3', audience: undefined }), 'recipient-unknown');
    assert.equal(h.offer({ platform: 'zoom-sdk', id: '4', audience: undefined }), 'recipient-unknown');
    assert.equal(h.offer({ platform: 'zoom-sdk', id: '5', audience: 'everyone' }), 'captured');
    assert.equal(h.sent.length, 2);
});

test('a Zoom label whose recipient cannot be read, and an SDK message naming no receiver, are not added', () => {
    const h = harness();
    // A label in another language is in neither English shape: no recipient.
    const label = parseZoomChatLabel('Bo Chen an Alle: Hallo zusammen');
    assert.equal(zoomChatAudience(label.recipient), undefined);
    assert.equal(h.offer({ id: 'z1', audience: zoomChatAudience(label.recipient) }), 'recipient-unknown');
    assert.equal(zoomSdkChatAudience(undefined, undefined), undefined);
    assert.equal(h.offer({ platform: 'zoom-sdk', id: 's1', audience: zoomSdkChatAudience(undefined, '') }), 'recipient-unknown');
    assert.equal(h.offer({ platform: 'zoom-sdk', id: 's2', audience: zoomSdkChatAudience(0, undefined) }), 'captured');
    assert.equal(h.sent.length, 1);
});

test('the debug line names a message whose recipient was not read', () => {
    setLogLevel('debug');
    const captured = captureLogs();
    try {
        const h = harness();
        h.offer({ id: 'u1', audience: undefined, text: 'just between us' });
        const lines = captured.messages().join('\n');
        assert.match(lines, /platform=zoom outcome=recipient-unknown sender=yes id=yes time=no recipient=unknown/);
        assert.ok(!lines.includes('between us') && !lines.includes('Bo Chen'), lines);
    } finally {
        captured.restore();
        setLogLevel(undefined);
    }
});

test('the Teams and Google Meet handlers say every message they offer was sent to everyone', () => {
    for (const file of ['teams.ts', 'teams-sdk.ts', 'google-meet.ts']) {
        const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        const offers = source.split('meetingChat.offer({').slice(1);
        assert.ok(offers.length > 0, `${file} offers no chat`);
        for (const offer of offers) {
            assert.ok(/audience: 'everyone'/.test(offer.slice(0, offer.indexOf('})'))), `${file} offers chat without a recipient`);
        }
    }
});

test('a message is added once by its platform id, and the same words under another id are added again', () => {
    const h = harness();
    assert.equal(h.offer({ id: 'm1', text: '+1' }), 'captured');
    assert.equal(h.offer({ id: 'm1', text: '+1' }), 'duplicate');
    assert.equal(h.offer({ id: 'm2', text: '+1' }), 'captured');
    assert.deepEqual(
        h.sent.map((s) => s.segmentId),
        ['chat-zoom-m1', 'chat-zoom-m2'],
    );
});

test('without an id, the same sender and text read again soon is a duplicate, and later is a new message', () => {
    const h = harness();
    assert.equal(h.offer({ text: '+1' }), 'captured');
    h.state.now += ID_LESS_DUPLICATE_MS - 1;
    assert.equal(h.offer({ text: '+1' }), 'duplicate');
    assert.equal(h.offer({ text: '+1', sender: 'Someone else' }), 'captured');
    h.state.now += ID_LESS_DUPLICATE_MS;
    assert.equal(h.offer({ text: '+1' }), 'captured');
    const [first, , third] = h.sent;
    assert.match(first.segmentId, /^chat-zoom-[0-9a-f]{24}$/);
    assert.notEqual(first.segmentId, third.segmentId, 'a repeated message must be its own row');
});

test('the chat already in the panel when the VP first read it is not added', () => {
    const h = harness();
    assert.equal(h.offer({ id: 'old', history: true }), 'history');
    assert.equal(h.offer({ id: 'old' }), 'duplicate');
    assert.equal(h.sent.length, 0);
});

// ---------------------------------------------------------------------------
// The gates
// ---------------------------------------------------------------------------

test('nothing is added while paused or while consent is withheld, nor afterwards', () => {
    const h = harness();
    h.state.capturing = false;
    assert.equal(h.offer({ id: 'while-paused' }), 'paused');
    h.state.capturing = true;
    assert.equal(h.offer({ id: 'while-paused' }), 'duplicate', 'a message read while paused was kept for later');
    assert.equal(h.sent.length, 0);
});

test('nothing is added before the meeting START has been sent', () => {
    const h = harness();
    h.state.started = false;
    assert.equal(h.offer({ id: 'early' }), 'not-started');
    h.state.started = true;
    assert.equal(h.offer({ id: 'later' }), 'captured');
});

test('nothing is added with the setting off, or while the VP redacts its transcript', () => {
    const h = harness();
    h.state.settingOn = false;
    assert.equal(h.offer({ id: 'a' }), 'disabled');
    h.state.settingOn = true;
    h.state.redacting = true;
    assert.equal(h.offer({ id: 'b' }), 'disabled');
    assert.equal(h.sent.length, 0);
});

test('redaction stops the capture whatever the language: the saved flag is read, not the en-US one', () => {
    // Saved on, meeting in Spanish: the VP does not redact its speech, but the chat stays out.
    assert.equal(contentRedactionRequested({ CONTENT_REDACTION_REQUESTED: 'true', ENABLE_CONTENT_REDACTION: 'false' }), true);
    assert.equal(contentRedactionRequested({ CONTENT_REDACTION_REQUESTED: 'false', ENABLE_CONTENT_REDACTION: 'false' }), false);
    assert.equal(contentRedactionRequested({ CONTENT_REDACTION_REQUESTED: 'false', ENABLE_CONTENT_REDACTION: 'true' }), false);
});

test('with no saved redaction choice applied, redaction is not known, unless the task variable says it is on', () => {
    // The task's combined variable is "false" for any language but en-US, so it never says redaction is off.
    assert.equal(contentRedactionRequested({ ENABLE_CONTENT_REDACTION: 'false' }), undefined);
    assert.equal(contentRedactionRequested({}), undefined);
    assert.equal(contentRedactionRequested({ CONTENT_REDACTION_REQUESTED: '' }), undefined);
    assert.equal(contentRedactionRequested({ ENABLE_CONTENT_REDACTION: 'true' }), true);
});

test('nothing is added when the configuration was not applied, and the reason is logged', () => {
    setLogLevel('debug');
    const captured = captureLogs();
    try {
        const h = harness({ redacting: () => undefined });
        assert.equal(h.offer({ id: 'a' }), 'redaction-unknown');
        assert.equal(h.offer({ id: 'b' }), 'redaction-unknown');
        assert.equal(h.sent.length, 0);
        const lines = captured.messages().join('\n');
        assert.match(lines, /outcome=redaction-unknown/);
        assert.match(lines, /redaction not known, the configuration was not applied/);
    } finally {
        captured.restore();
        setLogLevel(undefined);
    }
});

test('with the setting off, the outcome is disabled whether or not redaction is known', () => {
    const h = harness({ settingOn: () => false, redacting: () => undefined });
    assert.equal(h.offer({ id: 'a' }), 'disabled');
});

test('the capture this VP runs reads the redaction choice from contentRedactionRequested', () => {
    const source = readFileSync(new URL('../src/meeting-chat-runtime.ts', import.meta.url), 'utf8');
    assert.match(source, /redacting: \(\) => contentRedactionRequested\(\)/);
});

test('a platform the capture does not cover is observed for the diagnostics and nothing is added', () => {
    setLogLevel('debug');
    const captured = captureLogs();
    try {
        const h = harness();
        h.capture.observe({ platform: 'webex', text: 'just between us', sender: 'Bo Chen', id: 'w1', audience: 'direct' });
        assert.equal(h.sent.length, 0);
        assert.equal(h.capture.capturedCount, 0);
        const lines = captured.messages().join('\n');
        assert.match(lines, /platform=webex outcome=not-covered sender=yes id=yes time=no recipient=direct links=0/);
        assert.ok(!lines.includes('between us') && !lines.includes('Bo Chen'), lines);
    } finally {
        captured.restore();
        setLogLevel(undefined);
    }
});

test('the setting is on unless the configuration turned it off', () => {
    assert.equal(CAPTURE_MEETING_CHAT_DEFAULT, true);
    assert.equal(captureMeetingChatSetting({}), true);
    assert.equal(captureMeetingChatSetting({ CAPTURE_MEETING_CHAT: 'false' }), false);
    assert.equal(captureMeetingChatSetting({ CAPTURE_MEETING_CHAT: 'FALSE ' }), false);
    assert.equal(captureMeetingChatSetting({ CAPTURE_MEETING_CHAT: 'true' }), true);
    assert.equal(captureMeetingChatSetting({ CAPTURE_MEETING_CHAT: 'nonsense' }), true);
});

test('at most MAX_PER_MINUTE messages are added in a minute, and more once it has passed', () => {
    const h = harness();
    for (let i = 0; i < MAX_PER_MINUTE; i += 1) {
        assert.equal(h.offer({ id: `m${i}` }), 'captured');
    }
    assert.equal(h.offer({ id: 'over' }), 'flood');
    h.state.now += 60_000;
    assert.equal(h.offer({ id: 'next-minute' }), 'captured');
    assert.equal(h.sent.length, MAX_PER_MINUTE + 1);
});

test('at most MAX_PER_MEETING messages are added in one meeting', () => {
    const h = harness();
    for (let i = 0; i < MAX_PER_MEETING; i += 1) {
        if (i % MAX_PER_MINUTE === 0) h.state.now += 60_000;
        h.offer({ id: `m${i}` });
    }
    assert.equal(h.capture.capturedCount, MAX_PER_MEETING);
    h.state.now += 60_000;
    assert.equal(h.offer({ id: 'one-more' }), 'flood');
});

// ---------------------------------------------------------------------------
// The segment
// ---------------------------------------------------------------------------

test('a chat segment is a final MEETING_CHAT row at the moment it was read, under its sender', () => {
    const h = harness();
    h.state.timeline = 83.25;
    h.offer({ platform: 'google-meet', id: 'spaces/x/messages/1', text: '  See the doc  ', sender: '  Bo   Chen ' });
    assert.deepEqual(h.sent[0], {
        channel: 'MEETING_CHAT',
        // Not a safe id as given, so it is hashed.
        segmentId: h.sent[0].segmentId,
        startTime: 83.25,
        endTime: 83.25,
        transcript: 'See the doc',
        isPartial: false,
        speaker: 'Bo Chen',
    });
    assert.match(h.sent[0].segmentId, /^chat-google-meet-[0-9a-f]{24}$/);
});

test('a message with no sender shown is added as Unknown sender, never with an empty speaker', () => {
    const h = harness();
    h.offer({ sender: null, id: 'x' });
    h.offer({ sender: '   ', id: 'y' });
    assert.deepEqual(
        h.sent.map((s) => s.speaker),
        [UNKNOWN_CHAT_SENDER, UNKNOWN_CHAT_SENDER],
    );
});

test('long text is cut, and links the text did not show are added after it', () => {
    const long = 'x'.repeat(MAX_CHAT_TEXT_LENGTH + 50);
    assert.equal(chatTranscript(long).length, MAX_CHAT_TEXT_LENGTH);
    assert.ok(chatTranscript(long).endsWith('…'));
    assert.equal(
        chatTranscript('the plan', ['https://example.com/plan', 'javascript:alert(1)', 'https://example.com/plan', 'mailto:a@b']),
        'the plan (link: https://example.com/plan)',
    );
    assert.equal(chatTranscript('see https://example.com/a', ['https://example.com/a']), 'see https://example.com/a');
});

test('the capture logs no message text and no sender name, even at debug', () => {
    setLogLevel('debug');
    const captured = captureLogs();
    try {
        const h = harness();
        h.offer({ text: 'the merger closes on Friday', sender: 'Bo Chen', id: 'q', links: ['https://example.com/x'] });
        h.offer({ text: 'the merger closes on Friday', sender: 'Bo Chen', id: 'q' });
        h.state.settingOn = false;
        h.offer({ text: 'another secret line', sender: 'Bo Chen', id: 'r' });
        const lines = captured.messages().join('\n');
        assert.ok(!lines.includes('merger'), lines);
        assert.ok(!lines.includes('secret'), lines);
        assert.ok(!lines.includes('Bo Chen'), lines);
        assert.match(lines, /platform=zoom outcome=captured sender=yes id=yes time=no recipient=everyone links=1/);
    } finally {
        captured.restore();
        setLogLevel(undefined);
    }
});

// ---------------------------------------------------------------------------
// The Kinesis record
// ---------------------------------------------------------------------------

describe('sendChatSegment', () => {
    type Sent = Record<string, unknown>;
    const records: Sent[] = [];
    const manager = kinesisStreamManager as unknown as { sendRecord: (record: Sent) => Promise<void> };
    let original: (record: Sent) => Promise<void>;

    beforeEach(() => {
        records.length = 0;
        original = manager.sendRecord;
        manager.sendRecord = async (record: Sent) => {
            records.push(record);
        };
    });
    afterEach(() => {
        manager.sendRecord = original;
    });

    it('writes an ADD_TRANSCRIPT_SEGMENT on the MEETING_CHAT channel, final, with its sender as the speaker', async () => {
        await kinesisStreamManager.sendChatSegment({
            channel: 'REMOTE',
            segmentId: 'chat-zoom-1',
            startTime: 4,
            endTime: 4,
            transcript: 'hello',
            isPartial: true,
            speaker: 'Bo Chen',
        });
        assert.equal(records.length, 1);
        const record = records[0];
        assert.equal(record.EventType, 'ADD_TRANSCRIPT_SEGMENT');
        assert.equal(record.Channel, 'MEETING_CHAT');
        assert.equal(record.IsPartial, false);
        assert.equal(record.SegmentId, 'chat-zoom-1');
        assert.equal(record.StartTime, 4);
        assert.equal(record.EndTime, 4);
        assert.equal(record.Speaker, 'Bo Chen');
        assert.equal(record.Transcript, 'hello');
        assert.equal(record.CallId, kinesisStreamManager.currentCallId);
    });

    it('reports the meeting started only once its START has been sent', async () => {
        assert.equal(kinesisStreamManager.meetingStarted, false);
        await kinesisStreamManager.sendStartMeeting();
        assert.equal(kinesisStreamManager.meetingStarted, true);
        assert.equal(records[0].EventType, 'START');
    });
});

// ---------------------------------------------------------------------------
// The platforms' recipients
// ---------------------------------------------------------------------------

test('a Zoom label gives its recipient and time, and a message to anyone but everyone is private', () => {
    assert.deepEqual(parseZoomChatLabel('Alice Smith to Everyone, 10:42 AM, see the doc'), {
        sender: 'Alice Smith',
        recipient: 'Everyone',
        body: 'see the doc',
        time: '10:42 AM',
    });
    assert.deepEqual(parseZoomChatLabel('Bob to You: just between us'), {
        sender: 'Bob',
        recipient: 'You',
        body: 'just between us',
        time: null,
    });
    assert.equal(zoomChatAudience('Everyone'), 'everyone');
    assert.equal(zoomChatAudience('Everyone (in Meeting)'), 'everyone');
    assert.equal(zoomChatAudience('You'), 'direct');
    assert.equal(zoomChatAudience('You (Direct Message)'), 'direct');
    assert.equal(zoomChatAudience('Everyone (Privately)'), 'direct');
    assert.equal(zoomChatAudience('Carol'), 'direct');
    assert.equal(zoomChatAudience(null), undefined);
});

test('a Zoom SDK message to receiver 0 or "Everyone" is public, to anyone else private, and unknown without a receiver', () => {
    assert.equal(zoomSdkChatAudience(0, null), 'everyone');
    assert.equal(zoomSdkChatAudience('0', undefined), 'everyone');
    assert.equal(zoomSdkChatAudience(16778240, null), 'direct');
    assert.equal(zoomSdkChatAudience(16778240, 'Everyone'), 'everyone');
    assert.equal(zoomSdkChatAudience(0, 'Bob'), 'direct');
    assert.equal(zoomSdkChatAudience(undefined, null), undefined);
    assert.equal(zoomSdkChatAudience('', ''), undefined);
});
