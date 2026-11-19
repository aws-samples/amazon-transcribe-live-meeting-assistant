/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Chat attachments (chat-attachments.ts, chat-attachments-page.ts): which are
 * taken, how their type is decided, what is stored and announced, and how the
 * Teams page is read. The Zoom rows are read in zoom-chat.test.ts, with that
 * file's fake of the Zoom chat panel.
 *
 * What is NOT covered here: the live platforms (whether Zoom still serves a
 * file card as a download, whether Teams still draws a pasted image with a
 * `blob:` address). Those are measured in live meetings; see
 * docs/virtual-participant.md, "Chat attachments".
 */
import { strict as assert } from 'node:assert';
import { mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import {
    ALLOWED_CONTENT_TYPES,
    AttachmentFetchResult,
    CAPTURE_CHAT_ATTACHMENTS_DEFAULT,
    CHAT_ATTACHMENTS_PREFIX,
    ChatAttachmentCapture,
    ChatAttachmentDeps,
    ChatAttachmentEvent,
    IncomingChatAttachment,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    attachmentIdFor,
    attachmentKey,
    captureChatAttachmentsSetting,
    chatAttachmentSummariesSetting,
    displayFileName,
    safeKeyName,
    sniffContentType,
} from './chat-attachments.js';
import {
    fetchBlobInPage,
    readDownload,
    readTeamsChatAttachmentsInPage,
    TEAMS_ATTACHMENT_SELECTORS,
    teamsAttachmentChats,
} from './chat-attachments-page.js';
import { IncomingChat, LATE_ATTACHMENT_GRACE_MS, LATE_ATTACHMENT_WINDOW_MS, MeetingChatCapture, chatTranscriptWithAttachments } from './meeting-chat.js';
import { OwnChatLedger } from './meeting-chat.js';
import type { TranscriptSegment } from './kinesis-stream.js';
import { captureLogs, setLogLevel } from './logger.js';

// ---------------------------------------------------------------------------
// Bytes of each kind
// ---------------------------------------------------------------------------

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13]);
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 16]);
const PDF = Buffer.from('%PDF-1.7\n1 0 obj\n');
/** A ZIP that names the parts of a Word document, as an Office Open XML file does. */
const DOCX = Buffer.concat([Buffer.from([0x50, 0x4b, 0x03, 0x04]), Buffer.from('[Content_Types].xml word/document.xml')]);
const ZIP = Buffer.concat([Buffer.from([0x50, 0x4b, 0x03, 0x04]), Buffer.from('payload.bin')]);
const OLE = Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1, 0, 0]);

test('an attachment’s type is read from its leading bytes', () => {
    assert.equal(sniffContentType(PNG, 'photo.png'), 'image/png');
    assert.equal(sniffContentType(JPEG, 'whatever.bin'), 'image/jpeg');
    assert.equal(sniffContentType(Buffer.from('GIF89a....'), 'a.gif'), 'image/gif');
    assert.equal(sniffContentType(Buffer.from('RIFF\0\0\0\0WEBPVP8 '), 'a.webp'), 'image/webp');
    assert.equal(sniffContentType(PDF, 'report.pdf'), 'application/pdf');
    assert.equal(sniffContentType(PDF, 'report.txt'), 'application/pdf', 'the bytes decide, not the name');
});

test('a file named as an image that is not one is not taken as an image', () => {
    assert.equal(sniffContentType(Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>'), 'logo.png'), null);
    assert.equal(sniffContentType(Buffer.from('MZ\x90\x00'), 'photo.jpg'), null);
});

test('SVG, HTML and archives are not allowed types', () => {
    assert.equal(sniffContentType(Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>'), 'logo.svg'), null);
    assert.equal(sniffContentType(Buffer.from('<!doctype html><html>'), 'page.html'), null);
    assert.equal(sniffContentType(ZIP, 'bundle.zip'), null);
    for (const type of ALLOWED_CONTENT_TYPES) {
        assert.ok(!/svg|html|javascript|zip|octet/.test(type), type);
    }
});

test('an Office document is told apart by its parts, and a ZIP named .docx is not one', () => {
    assert.equal(sniffContentType(DOCX, 'notes.docx'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
    assert.equal(sniffContentType(ZIP, 'notes.docx'), null);
    assert.equal(sniffContentType(DOCX, 'notes.xlsx'), null, 'a Word document named as a workbook');
    assert.equal(sniffContentType(OLE, 'old.doc'), 'application/msword');
    assert.equal(sniffContentType(OLE, 'old.msi'), null);
});

test('plain text, CSV and Markdown are text only when the bytes are text', () => {
    assert.equal(sniffContentType(Buffer.from('a,b\n1,2\n'), 'data.csv'), 'text/csv');
    assert.equal(sniffContentType(Buffer.from('# Notes\n'), 'notes.md'), 'text/markdown');
    assert.equal(sniffContentType(Buffer.from('hello'), 'notes.txt'), 'text/plain');
    assert.equal(sniffContentType(Buffer.from([0x68, 0x00, 0x69]), 'notes.txt'), null);
    assert.equal(sniffContentType(Buffer.from([0xc3, 0x28]), 'notes.txt'), null, 'not UTF-8');
    assert.equal(sniffContentType(Buffer.from('echo hi'), 'run.sh'), null);
});

test('the object key is inside the meeting’s folder and made of safe characters', () => {
    const key = attachmentKey('Weekly sync: 2026/11/19', '0000012500-ab12cd34', '../../Q3 plan (final).pdf');
    assert.equal(key, `${CHAT_ATTACHMENTS_PREFIX}Weekly_sync__2026_11_19/0000012500-ab12cd34/Q3_plan_final_.pdf`);
    assert.ok(!key.includes('..'));
    assert.equal(safeKeyName(''), 'attachment');
    assert.equal(safeKeyName('....'), 'attachment');
    const long = safeKeyName(`${'x'.repeat(300)}.pdf`);
    assert.ok(long.length <= 120 && long.endsWith('.pdf'));
});

test('attachment ids sort in transcript time', () => {
    assert.equal(attachmentIdFor(12.5, 'aa'), '0000012500-aa');
    assert.ok(attachmentIdFor(9, 'zz') < attachmentIdFor(10, 'aa'));
});

test('the shown file name drops control characters and is never empty', () => {
    assert.equal(displayFileName('a\u0000b\nc.png', 'image'), 'a b c.png');
    assert.equal(displayFileName('', 'image'), 'image');
    assert.equal(displayFileName(null, 'document'), 'file');
});

test('capturing chat attachments is off unless the setting turns it on; titles are on by default', () => {
    assert.equal(CAPTURE_CHAT_ATTACHMENTS_DEFAULT, false);
    assert.equal(captureChatAttachmentsSetting({}), false);
    assert.equal(captureChatAttachmentsSetting({ CAPTURE_CHAT_ATTACHMENTS: 'true' }), true);
    assert.equal(captureChatAttachmentsSetting({ CAPTURE_CHAT_ATTACHMENTS: 'nonsense' }), false);
    assert.equal(chatAttachmentSummariesSetting({}), true);
    assert.equal(chatAttachmentSummariesSetting({ CHAT_ATTACHMENT_SUMMARIES: 'false' }), false);
});

// ---------------------------------------------------------------------------
// The capture: what is stored and announced
// ---------------------------------------------------------------------------

interface CaptureHarness {
    capture: ChatAttachmentCapture;
    uploads: Array<{ key: string; body: Buffer; contentType: string }>;
    events: ChatAttachmentEvent[];
    state: { settingOn: boolean; summariesOn: boolean; capturing: boolean };
}

function captureHarness(overrides: Partial<ChatAttachmentDeps> = {}): CaptureHarness {
    const state = { settingOn: true, summariesOn: true, capturing: true };
    const uploads: CaptureHarness['uploads'] = [];
    const events: ChatAttachmentEvent[] = [];
    let n = 0;
    const capture = new ChatAttachmentCapture({
        settingOn: () => state.settingOn,
        summariesOn: () => state.summariesOn,
        capturing: () => state.capturing,
        callId: () => 'Team sync-2026-11-19',
        upload: async (key, body, contentType) => {
            uploads.push({ key, body, contentType });
        },
        emit: async (event) => {
            events.push(event);
        },
        newId: () => `id-${(n += 1)}`,
        ...overrides,
    });
    return { capture, uploads, events, state };
}

const fetching = (bytes: Buffer): ((maxBytes: number) => Promise<AttachmentFetchResult>) => async (maxBytes) =>
    bytes.length > maxBytes ? { ok: false, reason: 'too-large', size: bytes.length } : { ok: true, bytes };

test('an allowed attachment is uploaded under the meeting’s folder and announced with its chat segment', async () => {
    const h = captureHarness();
    assert.equal(h.capture.capture('chat-zoom-m1', 'zoom', 12.5, [{ kind: 'image', name: 'whiteboard.png', fetch: fetching(PNG) }]), 1);
    await h.capture.idle();
    assert.equal(h.uploads.length, 1);
    assert.equal(h.uploads[0].key, `${CHAT_ATTACHMENTS_PREFIX}Team_sync_2026_11_19/id-1/whiteboard.png`);
    assert.equal(h.uploads[0].contentType, 'image/png');
    assert.deepEqual(h.events, [
        {
            AttachmentId: 'id-1',
            SegmentId: 'chat-zoom-m1',
            Timestamp: 12.5,
            FileName: 'whiteboard.png',
            Platform: 'zoom',
            Kind: 'IMAGE',
            Status: 'STORED',
            ContentType: 'image/png',
            Size: PNG.length,
            S3Key: h.uploads[0].key,
            Summarize: true,
        },
    ]);
});

test('the AI title switch is carried on the announcement', async () => {
    const h = captureHarness();
    h.state.summariesOn = false;
    h.capture.capture('s', 'zoom', 1, [{ kind: 'document', name: 'r.pdf', fetch: fetching(PDF) }]);
    await h.capture.idle();
    assert.equal(h.events[0].Summarize, false);
    assert.equal(h.events[0].Kind, 'DOCUMENT');
});

test('nothing is taken while the attachment setting is off', async () => {
    const h = captureHarness();
    h.state.settingOn = false;
    let fetched = false;
    const taken = h.capture.capture('s', 'zoom', 1, [
        {
            kind: 'image',
            name: 'a.png',
            fetch: async () => {
                fetched = true;
                return { ok: true, bytes: PNG };
            },
        },
    ]);
    await h.capture.idle();
    assert.equal(taken, 0);
    assert.equal(fetched, false);
    assert.deepEqual(h.events, []);
});

test('an attachment over the size cap is named but not stored', async () => {
    const h = captureHarness();
    const big = Buffer.alloc(MAX_ATTACHMENT_BYTES + 1, 0x20);
    h.capture.capture('s', 'zoom', 1, [{ kind: 'document', name: 'huge.pdf', fetch: fetching(Buffer.concat([PDF, big])) }]);
    await h.capture.idle();
    assert.equal(h.uploads.length, 0);
    assert.equal(h.events[0].Status, 'NOT_STORED');
    assert.equal(h.events[0].NotStoredReason, 'TOO_LARGE');
    assert.equal(h.events[0].Summarize, false);
});

test('a file card showing a size over the cap is not clicked', async () => {
    const h = captureHarness();
    let fetched = false;
    h.capture.capture('s', 'zoom', 1, [
        {
            kind: 'document',
            name: 'huge.pdf',
            size: MAX_ATTACHMENT_BYTES * 2,
            fetch: async () => {
                fetched = true;
                return { ok: true, bytes: PDF };
            },
        },
    ]);
    await h.capture.idle();
    assert.equal(fetched, false);
    assert.equal(h.events[0].NotStoredReason, 'TOO_LARGE');
    assert.equal(h.events[0].Size, MAX_ATTACHMENT_BYTES * 2);
});

test('an attachment of a type not allowed is named but not stored', async () => {
    const h = captureHarness();
    h.capture.capture('s', 'zoom', 1, [{ kind: 'document', name: 'setup.exe', fetch: fetching(Buffer.from('MZ\x90\x00')) }]);
    await h.capture.idle();
    assert.equal(h.uploads.length, 0);
    assert.equal(h.events[0].NotStoredReason, 'TYPE_NOT_ALLOWED');
    assert.equal(h.events[0].ContentType, undefined);
    assert.equal(h.events[0].S3Key, undefined);
});

test('an attachment the platform does not let the VP download is named with its kind', async () => {
    const h = captureHarness();
    h.capture.capture('s', 'teams', 1, [{ kind: 'document', name: 'Plan.docx' }]);
    await h.capture.idle();
    assert.deepEqual(h.events[0], {
        AttachmentId: 'id-1',
        SegmentId: 's',
        Timestamp: 1,
        FileName: 'Plan.docx',
        Platform: 'teams',
        Kind: 'DOCUMENT',
        Status: 'NOT_STORED',
        NotStoredReason: 'NOT_DOWNLOADABLE',
        Summarize: false,
    });
});

test('a failed fetch or upload is recorded as unavailable', async () => {
    const h = captureHarness({
        upload: async () => {
            throw new Error('denied');
        },
    });
    h.capture.capture('s', 'zoom', 1, [
        {
            kind: 'image',
            name: 'a.png',
            fetch: async () => {
                throw new Error('gone');
            },
        },
        { kind: 'image', name: 'b.png', fetch: fetching(PNG) },
    ]);
    await h.capture.idle();
    assert.deepEqual(
        h.events.map((e) => e.NotStoredReason),
        ['UNAVAILABLE', 'UNAVAILABLE'],
    );
});

test('no log line names an attachment, its sender or what an error quotes of it', async () => {
    setLogLevel('debug');
    const captured = captureLogs();
    try {
        const h = captureHarness({
            upload: async (key) => {
                throw Object.assign(new Error(`not authorized to perform s3:PutObject on ${key}`), { name: 'AccessDenied' });
            },
        });
        h.capture.capture('s', 'zoom', 1, [
            { kind: 'image', name: 'secret-plan.png', fetch: fetching(PNG) },
            { kind: 'document', name: 'payroll.docx' },
        ]);
        await h.capture.idle();
        const lines = JSON.stringify(captured.records);
        assert.ok(!lines.includes('secret-plan') && !lines.includes('payroll'), lines);
        assert.match(lines, /could not upload an attachment \(AccessDenied\)/);
    } finally {
        captured.restore();
        setLogLevel('info');
    }
});

test('nothing is stored when capture stops while the attachment is fetched', async () => {
    const h = captureHarness();
    h.capture.capture('s', 'zoom', 1, [
        {
            kind: 'image',
            name: 'a.png',
            fetch: async () => {
                // Consent withdrawn, or the transcript paused, meanwhile.
                h.state.capturing = false;
                return { ok: true, bytes: PNG };
            },
        },
    ]);
    await h.capture.idle();
    assert.deepEqual(h.uploads, []);
    assert.deepEqual(h.events, []);
});

test('at most a fixed number of attachments is taken from one message', async () => {
    const h = captureHarness();
    const many: IncomingChatAttachment[] = Array.from({ length: MAX_ATTACHMENTS_PER_MESSAGE + 5 }, (_, i) => ({ kind: 'document', name: `f${i}.docx` }));
    assert.equal(h.capture.capture('s', 'teams', 1, many), MAX_ATTACHMENTS_PER_MESSAGE);
    await h.capture.idle();
    assert.equal(h.events.length, MAX_ATTACHMENTS_PER_MESSAGE);
});

test('attachments are handled one at a time, in the order offered', async () => {
    const h = captureHarness();
    const order: string[] = [];
    let running = 0;
    const slow = (name: string): IncomingChatAttachment => ({
        kind: 'image',
        name,
        fetch: async () => {
            running += 1;
            assert.equal(running, 1, 'two fetches at once');
            await new Promise((r) => setTimeout(r, 5));
            order.push(name);
            running -= 1;
            return { ok: true, bytes: PNG };
        },
    });
    h.capture.capture('s1', 'zoom', 1, [slow('one.png'), slow('two.png')]);
    h.capture.capture('s2', 'zoom', 2, [slow('three.png')]);
    await h.capture.idle();
    assert.deepEqual(order, ['one.png', 'two.png', 'three.png']);
});

// ---------------------------------------------------------------------------
// The chat capture hands the attachments over only for a message it adds
// ---------------------------------------------------------------------------

function chatHarness() {
    const state = { settingOn: true, redacting: false as boolean | undefined, capturing: true, started: true, now: 0 };
    const sent: TranscriptSegment[] = [];
    const taken: Array<{ segmentId: string; names: string[] }> = [];
    const capture = new MeetingChatCapture({
        settingOn: () => state.settingOn,
        redacting: () => state.redacting,
        capturing: () => state.capturing,
        meetingStarted: () => state.started,
        timelineSeconds: () => 30,
        send: async (segment) => {
            sent.push(segment);
        },
        ownIdentities: () => ['LMA'],
        ownName: () => 'LMA',
        ledger: new OwnChatLedger(() => state.now),
        now: () => state.now,
        attachments: (segmentId, _platform, _at, list) => {
            taken.push({ segmentId, names: list.map((a) => a.name) });
        },
    });
    const offer = (chat: Partial<IncomingChat>) =>
        capture.offer({ platform: 'zoom', text: '', sender: 'Bo Chen', audience: 'everyone', ...chat } as IncomingChat);
    return { capture, sent, taken, state, offer };
}

const IMAGE: IncomingChatAttachment = { kind: 'image', name: 'whiteboard.png' };

test('a message of an attachment alone is added, naming the attachment, and the attachment is handed over', () => {
    const h = chatHarness();
    assert.equal(h.offer({ id: 'm1', attachments: [IMAGE] }), 'captured');
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].transcript, '(attachment: whiteboard.png)');
    assert.deepEqual(h.taken, [{ segmentId: h.sent[0].segmentId, names: ['whiteboard.png'] }]);
});

test('a message with text and an attachment keeps its text and names the attachment', () => {
    const h = chatHarness();
    h.offer({ id: 'm1', text: 'see this', attachments: [IMAGE] });
    assert.equal(h.sent[0].transcript, 'see this (attachment: whiteboard.png)');
    assert.equal(chatTranscriptWithAttachments('hi', ['https://example.com/x'], [IMAGE]), 'hi (link: https://example.com/x) (attachment: whiteboard.png)');
});

test('attachments are not taken while consent is withheld or the transcript is paused', () => {
    const h = chatHarness();
    h.state.capturing = false;
    assert.equal(h.offer({ id: 'm1', attachments: [IMAGE] }), 'paused');
    assert.deepEqual(h.taken, []);
    assert.deepEqual(h.sent, []);
});

test('attachments are not taken while the meeting chat is not added to the transcript', () => {
    const h = chatHarness();
    h.state.settingOn = false;
    assert.equal(h.offer({ id: 'm1', attachments: [IMAGE] }), 'disabled');
    h.state.settingOn = true;
    h.state.redacting = true;
    assert.equal(h.offer({ id: 'm2', attachments: [IMAGE] }), 'disabled');
    assert.deepEqual(h.taken, []);
});

test('attachments of a private message, or one already in the chat, are not taken', () => {
    const h = chatHarness();
    assert.equal(h.offer({ id: 'm1', audience: 'direct', attachments: [IMAGE] }), 'direct');
    assert.equal(h.offer({ id: 'm2', history: true, attachments: [IMAGE] }), 'history');
    assert.equal(h.offer({ id: 'm3', sender: 'LMA', attachments: [IMAGE] }), 'own');
    assert.deepEqual(h.taken, []);
});

test('a message with neither text nor attachments is still empty', () => {
    const h = chatHarness();
    assert.equal(h.offer({ id: 'm1', attachments: [] }), 'empty');
});

// ---------------------------------------------------------------------------
// Attachments that appear after their message was read
// ---------------------------------------------------------------------------

const SHOT: IncomingChatAttachment = { kind: 'image', name: 'Screenshot.png' };

test('an attachment that appears after its message was added is added to the same line, once', () => {
    const h = chatHarness();
    assert.equal(h.offer({ id: 'm1', text: 'Screenshot pasted:' }), 'captured');
    assert.deepEqual(h.taken, []);
    h.state.now += 5_000;
    assert.equal(h.offer({ id: 'm1', text: 'Screenshot pasted:' }), 'duplicate');
    assert.equal(h.capture.offerLateAttachments({ platform: 'zoom', id: 'm1', text: 'Screenshot pasted:', sender: 'Bo Chen', audience: 'everyone', attachments: [SHOT] }), 'attachments-added');
    assert.equal(h.sent.length, 2, 'the line is written again');
    assert.equal(h.sent[1].segmentId, h.sent[0].segmentId, 'under the same segment id');
    assert.equal(h.sent[1].startTime, h.sent[0].startTime);
    assert.equal(h.sent[1].transcript, 'Screenshot pasted: (attachment: Screenshot.png)');
    assert.deepEqual(h.taken, [{ segmentId: h.sent[0].segmentId, names: ['Screenshot.png'] }]);
});

test('a late attachment is not taken once the message’s window has passed', () => {
    const h = chatHarness();
    h.offer({ id: 'm1', text: 'see below' });
    h.state.now += LATE_ATTACHMENT_WINDOW_MS + LATE_ATTACHMENT_GRACE_MS + 1;
    assert.equal(h.capture.offerLateAttachments({ platform: 'zoom', id: 'm1', text: 'see below', sender: 'Bo Chen', audience: 'everyone', attachments: [SHOT] }), 'duplicate');
    assert.deepEqual(h.taken, []);
    assert.equal(h.sent.length, 1);
});

test('an image the page reports as never loaded when its window closes is still recorded on its line', () => {
    const h = chatHarness();
    h.offer({ id: 'm1', text: 'here' });
    h.state.now += LATE_ATTACHMENT_WINDOW_MS + 2_000;
    assert.equal(h.capture.offerLateAttachments({ platform: 'zoom', id: 'm1', text: 'here', sender: 'Bo Chen', audience: 'everyone', attachments: [SHOT] }), 'attachments-added');
    assert.equal(h.taken.length, 1);
});

test('a late attachment is not taken while the meeting is paused, nor for a message left out', () => {
    const h = chatHarness();
    h.offer({ id: 'm1', text: 'hi' });
    h.state.capturing = false;
    assert.equal(h.capture.offerLateAttachments({ platform: 'zoom', id: 'm1', text: 'hi', audience: 'everyone', attachments: [SHOT] }), 'paused');
    h.state.capturing = true;
    assert.equal(h.offer({ id: 'm2', text: 'private', audience: 'direct' }), 'direct');
    assert.equal(h.capture.offerLateAttachments({ platform: 'zoom', id: 'm2', text: 'private', audience: 'direct', attachments: [SHOT] }), 'duplicate');
    assert.deepEqual(h.taken, []);
});

test('a message first read with nothing in it is added once its attachment appears', () => {
    const h = chatHarness();
    assert.equal(h.offer({ id: 'm1', text: '' }), 'empty');
    assert.equal(h.capture.offerLateAttachments({ platform: 'zoom', id: 'm1', text: '', sender: 'Bo Chen', audience: 'everyone', attachments: [SHOT] }), 'captured');
    assert.equal(h.sent[0].transcript, '(attachment: Screenshot.png)');
    assert.equal(h.taken.length, 1);
});

test('a second Teams image on a message is added to the line its first one made', () => {
    const h = chatHarness();
    const teams = (name: string): IncomingChat => ({ platform: 'teams', id: 'mid-attachments', text: '', sender: 'A', audience: 'everyone', attachments: [{ kind: 'image', name }] });
    assert.equal(h.capture.offerLateAttachments(teams('one.png')), 'captured');
    assert.equal(h.capture.offerLateAttachments(teams('two.png')), 'attachments-added');
    assert.equal(h.sent[1].transcript, '(attachment: one.png) (attachment: two.png)');
    assert.deepEqual(h.taken.map((t) => t.names), [['one.png'], ['two.png']]);
});

// ---------------------------------------------------------------------------
// In the page: the blob fetch
// ---------------------------------------------------------------------------

async function withFetch<T>(impl: (src: string) => Promise<Response>, fn: () => Promise<T>): Promise<T> {
    const g = globalThis as any;
    const real = g.fetch;
    g.fetch = impl;
    try {
        return await fn();
    } finally {
        g.fetch = real;
    }
}

test('the in-page fetch reads only blob: addresses', async () => {
    let called = false;
    const result = await withFetch(
        async () => {
            called = true;
            return new Response(PNG);
        },
        () => fetchBlobInPage({ src: 'https://example.com/a.png', maxBytes: 1000 }),
    );
    assert.deepEqual(result, { ok: false, reason: 'unavailable' });
    assert.equal(called, false);
});

test('the in-page fetch returns the bytes as base64 and refuses more than the cap', async () => {
    const ok = await withFetch(async () => new Response(PNG), () => fetchBlobInPage({ src: 'blob:https://teams.example/1', maxBytes: 1000 }));
    assert.equal(ok.ok, true);
    assert.deepEqual(ok.ok && Buffer.from(ok.base64, 'base64'), PNG);
    const big = await withFetch(async () => new Response(PNG), () => fetchBlobInPage({ src: 'blob:https://teams.example/1', maxBytes: 4 }));
    assert.deepEqual(big, { ok: false, reason: 'too-large', size: PNG.length });
    const missing = await withFetch(async () => new Response('', { status: 404 }), () => fetchBlobInPage({ src: 'blob:x', maxBytes: 4 }));
    assert.deepEqual(missing, { ok: false, reason: 'unavailable' });
});

test('a download is read back, held to the cap, and the browser’s copy deleted either way', async () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'lma-att-'));
    const file = path.join(dir, 'download');
    writeFileSync(file, PDF);
    let deleted = 0;
    const download = { failure: async () => null, path: async () => file, delete: async () => void (deleted += 1) };
    const ok = await readDownload(download, 1000);
    assert.deepEqual(ok, { ok: true, bytes: PDF });
    const big = await readDownload(download, 3);
    assert.deepEqual(big, { ok: false, reason: 'too-large', size: PDF.length });
    const failed = await readDownload({ ...download, failure: async () => 'canceled' }, 1000);
    assert.deepEqual(failed, { ok: false, reason: 'unavailable' });
    assert.equal(deleted, 3);
    assert.ok(existsSync(file), 'the test file is the fake download’s; delete() is what removes the real one');
});

// ---------------------------------------------------------------------------
// In the page: the Teams chat, against a fake DOM
// ---------------------------------------------------------------------------

type Attr = { name: string; op: '' | '=' | '^=' | '*='; value: string };

class FakeElement {
    parentElement: FakeElement | null = null;
    children: FakeElement[] = [];
    constructor(
        readonly tag: string,
        readonly attrs: Record<string, string> = {},
        children: Array<FakeElement | string> = [],
        private readonly ownText = '',
    ) {
        for (const c of children) {
            if (typeof c === 'string') this.ownText += c;
            else {
                c.parentElement = this;
                this.children.push(c);
            }
        }
    }
    get textContent(): string {
        return this.ownText + this.children.map((c) => c.textContent).join('');
    }
    getAttribute(name: string): string | null {
        return name in this.attrs ? this.attrs[name] : null;
    }
    private matchesOne(selector: string): boolean {
        const re = /^([a-z]+)|\[([\w-]+)(?:(\^=|\*=|=)"([^"]*)")?\]/gy;
        let tag: string | null = null;
        const attrs: Attr[] = [];
        let at = 0;
        let m: RegExpExecArray | null;
        while (at < selector.length && (m = re.exec(selector))) {
            if (m[1]) tag = m[1];
            else attrs.push({ name: m[2], op: (m[3] as Attr['op']) ?? '', value: m[4] ?? '' });
            at = re.lastIndex;
        }
        if (at !== selector.length) throw new Error(`fake DOM: unsupported selector ${selector}`);
        if (tag && tag !== this.tag) return false;
        return attrs.every(({ name, op, value }) => {
            const v = this.getAttribute(name);
            if (v === null) return false;
            if (op === '=') return v === value;
            if (op === '^=') return v.startsWith(value);
            if (op === '*=') return v.includes(value);
            return true;
        });
    }
    matches(selector: string): boolean {
        return selector.split(',').some((part) => this.matchesOne(part.trim()));
    }
    closest(selector: string): FakeElement | null {
        for (let e: FakeElement | null = this; e; e = e.parentElement) if (e.matches(selector)) return e;
        return null;
    }
    querySelectorAll(selector: string): FakeElement[] {
        const found: FakeElement[] = [];
        const walk = (e: FakeElement): void => {
            for (const c of e.children) {
                if (c.matches(selector)) found.push(c);
                walk(c);
            }
        };
        walk(this);
        return found;
    }
    querySelector(selector: string): FakeElement | null {
        return this.querySelectorAll(selector)[0] ?? null;
    }
}

const el = (tag: string, attrs: Record<string, string> = {}, children: Array<FakeElement | string> = []) => new FakeElement(tag, attrs, children);

/** One Teams light-meetings message row, with its author and what it holds. */
const teamsRow = (mid: string | null, author: string, body: FakeElement[]) =>
    el('div', mid ? { 'data-mid': mid } : { role: 'listitem' }, [el('span', { 'data-tid': 'message-author-name' }, [author]), ...body]);

function withTeamsPage(rows: FakeElement[], fn: (read: () => ReturnType<typeof readTeamsChatAttachmentsInPage>, body: FakeElement) => void): void {
    const g = globalThis as any;
    const body = el('body', {}, rows);
    g.window = {};
    g.document = {
        querySelectorAll: (s: string) => body.querySelectorAll(s),
        querySelector: (s: string) => body.querySelector(s),
    };
    try {
        fn(() => readTeamsChatAttachmentsInPage(TEAMS_ATTACHMENT_SELECTORS), body);
    } finally {
        delete g.window;
        delete g.document;
    }
}

test('a Teams pasted image and a file card are read with the message’s author, once each', () => {
    withTeamsPage(
        [
            teamsRow('1700000000001', 'Strahan, Bob', [
                el('div', { 'data-tid': 'file-attachment-grid' }, [el('img', { src: 'blob:https://teams.microsoft.com/abc', alt: 'whiteboard.png' })]),
            ]),
            teamsRow('1700000000002', 'Lee, Sarah', [el('div', { 'data-tid': 'file-chiclet-Q3 plan.docx' }, ['Q3 plan.docx'])]),
            teamsRow('1700000000003', 'Lee, Sarah', [el('div', { 'data-tid': 'messageBodyContent' }, ['just text'])]),
        ],
        (read) => {
            const rows = read();
            assert.equal(rows.length, 2);
            assert.equal(rows[0].id, '1700000000001');
            assert.equal(rows[0].author, 'Strahan, Bob');
            assert.deepEqual(rows[0].attachments, [{ kind: 'image', name: 'whiteboard.png', src: 'blob:https://teams.microsoft.com/abc' }]);
            assert.deepEqual(rows[1].attachments, [{ kind: 'document', name: 'Q3 plan.docx' }]);
            assert.deepEqual(read(), [], 'read again, nothing is new');
        },
    );
});

test('a Teams image not yet loaded is read once it has its blob: address', () => {
    const img = el('img', { src: '', alt: 'later.png' });
    withTeamsPage([teamsRow('m1', 'A', [el('div', { class: 'x file-attachment-grid y' }, [img])])], (read) => {
        assert.deepEqual(read(), []);
        img.attrs.src = 'blob:https://teams.microsoft.com/later';
        assert.equal(read()[0].attachments[0].name, 'later.png');
    });
});

test('a file card drawn in both copies of a Teams message is read once', () => {
    withTeamsPage(
        [
            teamsRow(null, 'A', [el('div', { 'data-tid': 'file-chiclet-notes.pdf' })]),
            teamsRow(null, 'A', [el('div', { 'data-tid': 'file-chiclet-notes.pdf' })]),
        ],
        (read) => {
            const rows = read();
            assert.equal(rows.length, 1);
            assert.equal(rows[0].attachments.length, 1);
        },
    );
});

test('Teams attachments are offered as a line of their own; only images are fetched', () => {
    const fetchers: string[] = [];
    const chats = teamsAttachmentChats(
        [
            { id: 'm1', author: 'A', time: null, keys: ['img:blob:x'], attachments: [{ kind: 'image', name: 'a.png', src: 'blob:x' }, { kind: 'document', name: 'b.docx' }] },
            { id: null, author: null, time: null, keys: ['file:|c.pdf'], attachments: [{ kind: 'document', name: 'c.pdf' }] },
        ],
        false,
        (src) => {
            fetchers.push(src);
            return async () => ({ ok: false, reason: 'unavailable' });
        },
    );
    assert.equal(chats.length, 2);
    assert.equal(chats[0].text, '');
    assert.equal(chats[0].id, 'm1-attachments');
    assert.equal(chats[0].audience, 'everyone');
    assert.equal(typeof chats[0].attachments?.[0].fetch, 'function');
    assert.equal(chats[0].attachments?.[1].fetch, undefined, 'a guest cannot download a Teams file');
    assert.equal(chats[1].id, 'attachments-file:|c.pdf');
    assert.deepEqual(fetchers, ['blob:x']);
});
