/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Recording a chat pause and resume on the meeting record
 * (transcription-pause.ts): the record UPDATE_TRANSCRIPTION_PAUSE carries, that
 * nothing is written before the meeting record exists, that a failed write
 * never reaches the platform handler, and that every platform handler records
 * both of its chat commands.
 *
 * Not covered: a live meeting. Whether each platform's chat shows the sender's
 * display name is what the platform scrapers already hand the dismissal
 * command; this module stores whatever they pass.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import {
    MAX_PAUSED_BY_LENGTH,
    pausedByName,
    recordTranscriptionPause,
    type TranscriptionPauseSink,
} from './transcription-pause.js';

type Sent = { paused: boolean; changedAt: string; pausedBy: string | null };

const AT = new Date('2026-11-12T10:15:00.000Z');

const fakeSink = (started = true): TranscriptionPauseSink & { sent: Sent[] } => {
    const sent: Sent[] = [];
    return {
        sent,
        meetingStarted: started,
        sendTranscriptionPause: async (paused, changedAt, pausedBy) => {
            sent.push({ paused, changedAt, pausedBy });
        },
    };
};

describe('recordTranscriptionPause', () => {
    it('records a pause with the time and the sender', async () => {
        const sink = fakeSink();
        await recordTranscriptionPause(true, '  Alice Example ', sink, () => AT);
        assert.deepEqual(sink.sent, [{ paused: true, changedAt: AT.toISOString(), pausedBy: 'Alice Example' }]);
    });

    it('records a pause with no sender when the platform shows none', async () => {
        const sink = fakeSink();
        await recordTranscriptionPause(true, null, sink, () => AT);
        await recordTranscriptionPause(true, '   ', sink, () => AT);
        assert.deepEqual(
            sink.sent.map((s) => s.pausedBy),
            [null, null],
        );
    });

    it('records a resume without a sender', async () => {
        const sink = fakeSink();
        await recordTranscriptionPause(false, 'Bob', sink, () => AT);
        assert.deepEqual(sink.sent, [{ paused: false, changedAt: AT.toISOString(), pausedBy: null }]);
    });

    it('writes nothing before the meeting record exists', async () => {
        const sink = fakeSink(false);
        await recordTranscriptionPause(true, 'Alice', sink, () => AT);
        assert.deepEqual(sink.sent, []);
    });

    it('resolves when the write fails, so the meeting goes on', async () => {
        const sink: TranscriptionPauseSink = {
            meetingStarted: true,
            sendTranscriptionPause: async () => {
                throw new Error('stream unavailable');
            },
        };
        await recordTranscriptionPause(true, 'Alice', sink, () => AT);
    });

    it('resolves when the write throws before returning a promise', async () => {
        const sink: TranscriptionPauseSink = {
            meetingStarted: true,
            sendTranscriptionPause: () => {
                throw new Error('not a promise');
            },
        };
        await recordTranscriptionPause(false, null, sink, () => AT);
    });
});

describe('pausedByName', () => {
    it('bounds a long name', () => {
        assert.equal(pausedByName('x'.repeat(MAX_PAUSED_BY_LENGTH + 50))?.length, MAX_PAUSED_BY_LENGTH);
    });

    it('reads a missing or blank name as none', () => {
        assert.equal(pausedByName(undefined), null);
        assert.equal(pausedByName(''), null);
    });
});

describe('the UPDATE_TRANSCRIPTION_PAUSE record', () => {
    const sent: Record<string, unknown>[] = [];
    const manager = kinesisStreamManager as unknown as {
        sendRecord: (record: Record<string, unknown>) => Promise<void>;
    };

    beforeEach(() => {
        sent.length = 0;
        manager.sendRecord = async (record) => {
            sent.push(record);
        };
    });

    it('carries the pause, its time and who paused', async () => {
        await kinesisStreamManager.sendTranscriptionPause(true, AT.toISOString(), 'Alice');
        assert.equal(sent.length, 1);
        assert.equal(sent[0].EventType, 'UPDATE_TRANSCRIPTION_PAUSE');
        assert.equal(sent[0].CallId, kinesisStreamManager.currentCallId);
        assert.equal(sent[0].Paused, true);
        assert.equal(sent[0].ChangedAt, AT.toISOString());
        assert.equal(sent[0].PausedBy, 'Alice');
    });

    it('carries a resume without a sender', async () => {
        await kinesisStreamManager.sendTranscriptionPause(false, AT.toISOString(), 'Alice');
        assert.equal(sent[0].Paused, false);
        assert.equal('PausedBy' in sent[0], false);
    });
});

describe('every platform handler', () => {
    // The handlers that answer `LMA pause` and `LMA start` in the meeting chat.
    const HANDLERS = ['chime.ts', 'google-meet.ts', 'teams.ts', 'teams-sdk.ts', 'webex.ts', 'zoom.ts', 'zoom-sdk.ts'];

    for (const file of HANDLERS) {
        it(`${file} records both chat commands right where it flips the switch`, () => {
            const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
            assert.match(source, /details\.start = false;\s*void recordTranscriptionPause\(true, /);
            assert.match(source, /details\.start = true;\s*void recordTranscriptionPause\(false, null\);/);
            // Once each: the consent hold and the dismissal also turn capture
            // off, and are not a chat pause.
            assert.equal(source.match(/recordTranscriptionPause\(true, /g)?.length, 1);
            assert.equal(source.match(/recordTranscriptionPause\(false, /g)?.length, 1);
        });
    }

    it('the consent flow does not record a chat pause', () => {
        for (const file of ['consent-runtime.ts', 'meeting-consent.ts']) {
            const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
            assert.equal(source.includes('recordTranscriptionPause'), false, file);
        }
    });
});
