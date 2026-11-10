/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How on-demand engine rows are named on a Virtual Participant meeting with
 * voice separation on, driven through the transcription service's own row
 * handler and the real Kinesis writer (only `sendRecord` is stubbed).
 *
 * The timings are synthetic but shaped like the two-person Zoom meeting the
 * fusion thresholds were chosen on: long turns from one person, short replies
 * from the other, and a page indicator that holds each switch for 2.7 s.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import type { AsrSegment } from './asr-microvm-client.js';
// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import { RosterTimeline } from './roster-timeline.js';
// eslint-disable-next-line import/first
import { transcriptionService } from './scribe.js';

type Row = { EventType: string; SegmentId?: string; Speaker?: string; IsPartial?: boolean };

const service = transcriptionService as unknown as {
    rosterTimeline: RosterTimeline;
    handleMeetingAsrSegment: (segment: AsrSegment) => void;
    speakerFusionSession: string | undefined;
};
const writer = kinesisStreamManager as unknown as { sendRecord: (record: Row) => Promise<void> };

const SAM = 'Sam Lee';
const ANA = 'Ana Ruiz';
const sent: Row[] = [];
const savedLag = process.env.ASR_ROSTER_LAG_SECONDS;

let counter = 0;
/** One final row from the engine, and the speaker the writer was given for it. */
const final = async (startSec: number, endSec: number, speaker: string | undefined, session = 'r0-g0'): Promise<string> => {
    counter += 1;
    service.handleMeetingAsrSegment({
        segmentId: `vp-remote-${session}-s${counter}`,
        startSec,
        endSec,
        text: 'words',
        isPartial: false,
        speaker,
        session,
    });
    // sendAsrTranscriptSegment is called without being awaited.
    await new Promise((resolve) => setImmediate(resolve));
    return sent[sent.length - 1]?.Speaker ?? '';
};

/** Sam speaks [start, end] as spk_1 and the page shows him from its start. */
const samTurn = async (start: number, end: number, session?: string): Promise<string> => {
    service.rosterTimeline.record(start, SAM);
    return final(start, end, 'spk_1', session);
};

beforeEach(() => {
    sent.length = 0;
    writer.sendRecord = async (record: Row) => {
        sent.push(record);
    };
    service.rosterTimeline = new RosterTimeline(['LMA (user@example.com)']);
    // Every test starts a fresh engine session, so no vote carries between tests.
    service.speakerFusionSession = undefined;
    process.env.ASR_ROSTER_LAG_SECONDS = '0';
});

afterEach(() => {
    if (savedLag === undefined) delete process.env.ASR_ROSTER_LAG_SECONDS;
    else process.env.ASR_ROSTER_LAG_SECONDS = savedLag;
});

describe('on-demand engine rows with voice separation on', () => {
    it('name a short reply by its voice while the page still shows the previous speaker', async () => {
        // Ana opens with enough speech to prove her voice.
        service.rosterTimeline.record(0, ANA);
        for (let t = 0; t < 30; t += 6) {
            assert.equal(await final(t, t + 5, 'spk_0'), `${ANA} (spk_0)`);
        }
        await samTurn(30, 55);
        // Ana's 1.5 s reply at 55.3 s: Zoom holds Sam for 2.7 s after he was
        // last switched to, so the page shows Sam for the whole reply.
        service.rosterTimeline.record(57.7, ANA);
        assert.equal(await final(55.3, 56.8, 'spk_0'), `${ANA} (spk_0)`);
        // Without the voice the page alone names it Sam.
        assert.equal(service.rosterTimeline.speakerFor(55.3, 56.8, 0), SAM);
    });

    it('start a new vote when the engine session reconnects, since voice ids are renumbered', async () => {
        service.rosterTimeline.record(0, ANA);
        for (let t = 0; t < 30; t += 6) {
            await final(t, t + 5, 'spk_0', 'r0-g0');
        }
        service.rosterTimeline.record(30, SAM);
        // After the reconnect spk_0 is whoever the new session heard first:
        // Sam here, and nothing proven carries over from the old session.
        assert.equal(await final(30, 31, 'spk_0', 'r0-g1'), `${SAM} (spk_0)`);
    });

    it('carry the bare page name when voice separation is off', async () => {
        service.rosterTimeline.record(0, SAM);
        assert.equal(await final(0, 4, undefined), SAM);
    });

    it('name a partial by the page and leave the vote alone', async () => {
        service.rosterTimeline.record(0, ANA);
        service.handleMeetingAsrSegment({
            segmentId: 'vp-remote-p1',
            startSec: 0,
            endSec: 40,
            text: 'words',
            isPartial: true,
            speaker: 'spk_0',
            session: 'r0-g0',
        });
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(sent[sent.length - 1]?.IsPartial, true);
        service.rosterTimeline.record(40, SAM);
        // Had the partial voted, spk_0 would be proven to Ana by now.
        assert.equal(await final(40, 42, 'spk_0'), `${SAM} (spk_0)`);
    });
});
