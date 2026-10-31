/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How Amazon Transcribe rows are named on a Virtual Participant meeting, driven
 * through the real transcription service clock and the real Kinesis writer
 * (only `sendRecord` is stubbed).
 *
 * The fixtures come from a three-person Zoom standup (Zoom web client, voice
 * separation on). Participant-change times and row times are the measured
 * ones, placed on the Transcribe audio clock using the assistant's own turns
 * as the anchor; words inside a row are spread across the row's measured span.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import { RosterTimeline } from './roster-timeline.js';
// eslint-disable-next-line import/first
import { rosterLagSeconds, transcriptionService } from './scribe.js';

type Row = { EventType: string; SegmentId?: string; Speaker?: string; Transcript?: string; IsPartial?: boolean };

const service = transcriptionService as unknown as {
    sessionAudioBytes: number;
    sessionChannels: number;
    sessionClockBase: number;
    transcribeTimeOffsetSeconds: number;
    isTranscribing: boolean;
    rosterTimeline: RosterTimeline;
    speakerChange: (speaker: string) => Promise<void>;
    rosterNameFor: (start: number, end: number) => string | undefined;
    handleTranscriptEvents: (response: unknown, stereo?: boolean) => Promise<void>;
};

const writer = kinesisStreamManager as unknown as {
    sendRecord: (record: Row) => Promise<void>;
    setDiarization: (on: boolean) => void;
    setRosterLookup: (lookup: ((start: number, end: number) => string | undefined) | null) => void;
    sendTranscriptSegment: (speaker: string, result: unknown, offset?: number) => Promise<number>;
};

const SAMPLE_RATE = 16000;
const sent: Row[] = [];
const savedLag = process.env.ASR_ROSTER_LAG_SECONDS;

/** The platform reports `name` when `seconds` of audio have been sent on a mono stream. */
const reportAt = async (seconds: number, name: string, channels = 1): Promise<void> => {
    service.sessionChannels = channels;
    service.sessionAudioBytes = Math.round(seconds * SAMPLE_RATE) * 2 * channels;
    await service.speakerChange(name);
};

/** A final result whose words are spread evenly over [start, end], all with one voice label. */
const finalResult = (resultId: string, text: string, start: number, end: number, label: string) => {
    const words = text.split(' ');
    const step = (end - start) / words.length;
    return {
        ResultId: resultId,
        IsPartial: false,
        Alternatives: [
            {
                Transcript: text,
                Items: words.map((content, i) => ({
                    Content: content,
                    Type: 'pronunciation',
                    StartTime: start + i * step,
                    EndTime: start + (i + 1) * step,
                    Speaker: label,
                })),
            },
        ],
    };
};

const emit = async (result: unknown, emittedUnder = 'none'): Promise<Array<[string, string]>> => {
    sent.length = 0;
    await writer.sendTranscriptSegment(emittedUnder, result, 0);
    return sent
        .filter((row) => row.EventType === 'ADD_TRANSCRIPT_SEGMENT')
        .map((row) => [row.Speaker ?? '', row.Transcript ?? '']);
};

beforeEach(() => {
    sent.length = 0;
    writer.sendRecord = async (record: Row) => {
        sent.push(record);
    };
    service.rosterTimeline = new RosterTimeline(['LMA (user@example.com)']);
    service.sessionClockBase = 0;
    service.sessionAudioBytes = 0;
    service.sessionChannels = 1;
    writer.setRosterLookup((start, end) => service.rosterNameFor(start, end));
    writer.setDiarization(true);
    // The Zoom web client's allowance, pinned so the fixtures do not depend on
    // the platform this test process happens to resolve.
    process.env.ASR_ROSTER_LAG_SECONDS = '0';
});

afterEach(() => {
    writer.setRosterLookup(null);
    writer.setDiarization(false);
    if (savedLag === undefined) delete process.env.ASR_ROSTER_LAG_SECONDS;
    else process.env.ASR_ROSTER_LAG_SECONDS = savedLag;
});

describe('a participant change is placed on the Transcribe audio clock', () => {
    it('at the audio sent so far, when the platform reports it', async () => {
        await reportAt(7.3, 'Sam Lee');
        assert.equal(service.rosterTimeline.speakerAt(7.29), 'Unknown');
        assert.equal(service.rosterTimeline.speakerAt(7.3), 'Sam Lee');
    });

    it('counting both channels of a two-channel stream as one second of audio', async () => {
        await reportAt(5, 'Sam Lee', 2);
        assert.equal(service.rosterTimeline.speakerAt(4.99), 'Unknown');
        assert.equal(service.rosterTimeline.speakerAt(5), 'Sam Lee');
    });

    it('continuing the meeting timeline after a reconnect', async () => {
        service.sessionClockBase = 300;
        await reportAt(2, 'Ana Ruiz');
        assert.equal(service.rosterTimeline.speakerAt(301.9), 'Unknown');
        assert.equal(service.rosterTimeline.speakerAt(302), 'Ana Ruiz');
    });

    it('and a session advances the timeline over all the audio it was sent, not just its last word', async () => {
        service.transcribeTimeOffsetSeconds = 100;
        service.sessionAudioBytes = 30 * SAMPLE_RATE * 2;
        service.sessionChannels = 1;
        service.isTranscribing = true;
        try {
            await service.handleTranscriptEvents({ TranscriptResultStream: [] }, false);
        } finally {
            service.isTranscribing = false;
        }
        assert.equal(service.transcribeTimeOffsetSeconds, 130);
    });
});

describe('rows from the Zoom standup', () => {
    it("keep the previous speaker's last word when the next speaker is reported after it", async () => {
        // "Make sure that your stuff actually got merged in." -- Sam, 12.2-14.6 s.
        // Priya was reported at 14.9 s. Stamped at the last word of the result
        // in hand when the change was seen, the change landed at 14.1 s and
        // "in." became a row of Priya's.
        await reportAt(7.3, 'Sam Lee');
        await reportAt(14.9, 'Priya Patel');
        const rows = await emit(
            finalResult('650302330', 'Make sure that your stuff actually got merged in.', 12.2, 14.6, '3'),
            'Priya Patel',
        );
        assert.deepEqual(rows, [['Sam Lee (spk_3)', 'Make sure that your stuff actually got merged in.']]);
    });

    it('give a word straddling a change to whoever was shown for most of it', async () => {
        // "the new soft former" -- Priya; "former" ran 78.0-78.8 s and Sam was
        // reported at 78.6 s.
        await reportAt(64.1, 'Priya Patel');
        await reportAt(78.6, 'Sam Lee');
        const result = {
            ResultId: 'd1660c647',
            IsPartial: false,
            Alternatives: [
                {
                    Transcript: 'the new soft former',
                    Items: [
                        { Content: 'the', Type: 'pronunciation', StartTime: 77.3, EndTime: 77.5, Speaker: '2' },
                        { Content: 'new', Type: 'pronunciation', StartTime: 77.5, EndTime: 77.75, Speaker: '2' },
                        { Content: 'soft', Type: 'pronunciation', StartTime: 77.75, EndTime: 78.0, Speaker: '2' },
                        { Content: 'former', Type: 'pronunciation', StartTime: 78.0, EndTime: 78.8, Speaker: '2' },
                    ],
                },
            ],
        };
        assert.deepEqual(await emit(result, 'Sam Lee'), [['Priya Patel (spk_2)', 'the new soft former']]);
    });

    it('never show one voice label under two names', async () => {
        // On the call, Transcribe's spk_3 coincided with Sam for most of the
        // meeting and with Ana and Priya too. The platform's name is kept
        // for every row; the label is shown only with the participant it has
        // coincided with for the most speech.
        await reportAt(28.3, 'Sam Lee');
        await reportAt(123.3, 'Ana Ruiz');
        await reportAt(132.7, 'Priya Patel');
        await reportAt(143.6, 'Sam Lee');
        await reportAt(158.0, 'Ana Ruiz');
        await reportAt(169.0, 'Sam Lee');
        const rows = [
            ...(await emit(finalResult('r1', 'OK. So this is still the old camp', 28.5, 32.8, '3'))),
            ...(await emit(finalResult('r2', 'so maybe I have not actually pulled it', 33.2, 35.8, '3'))),
            ...(await emit(finalResult('r3', 'go first. My name is Ana.', 124.9, 126.8, '0'))),
            ...(await emit(finalResult('r4', 'My name is Priya', 134.1, 136.1, '2'))),
            ...(await emit(finalResult('r5', 'Nice, and I am Sam.', 144.7, 147.3, '3'))),
            ...(await emit(finalResult('r6', 'It almost seems like the transcript is not getting.', 159.1, 161.5, '3'))),
            ...(await emit(finalResult('r7', 'properly updated', 162.2, 164.0, '0'))),
            ...(await emit(finalResult('r8', 'Dynamo', 169.3, 170.4, '0'))),
        ].map(([speaker]) => speaker);
        assert.deepEqual(rows, [
            'Sam Lee (spk_3)',
            'Sam Lee (spk_3)',
            'Ana Ruiz (spk_0)',
            'Priya Patel (spk_2)',
            'Sam Lee (spk_3)',
            'Ana Ruiz',
            'Ana Ruiz (spk_0)',
            'Sam Lee',
        ]);
        const namesPerLabel = new Map<string, Set<string>>();
        for (const speaker of rows) {
            const match = /^(.*) \((spk_\d+)\)$/.exec(speaker);
            if (match) {
                namesPerLabel.set(match[2], (namesPerLabel.get(match[2]) ?? new Set()).add(match[1]));
            }
        }
        for (const [label, names] of namesPerLabel) {
            assert.equal(names.size, 1, `${label} shown under ${[...names].join(', ')}`);
        }
    });

    it('still separate the voices behind one participant', async () => {
        await reportAt(0, 'Conference Room');
        const rows = [
            ...(await emit(finalResult('c1', 'we tried the new build this morning', 1, 4, '0'))),
            ...(await emit(finalResult('c2', 'and it fixed the stall we saw', 4.5, 7, '1'))),
            ...(await emit(finalResult('c3', 'so we can ship it', 7.5, 9, '0'))),
        ].map(([speaker]) => speaker);
        assert.deepEqual(rows, ['Conference Room (spk_0)', 'Conference Room (spk_1)', 'Conference Room (spk_0)']);
    });

    it('are named by the platform alone when voice separation is off', async () => {
        writer.setDiarization(false);
        await reportAt(7.3, 'Sam Lee');
        await reportAt(14.9, 'Priya Patel');
        const rows = await emit(
            finalResult('650302330', 'Make sure that your stuff actually got merged in.', 12.2, 14.6, '3'),
            'Priya Patel',
        );
        assert.deepEqual(rows, [['Sam Lee', 'Make sure that your stuff actually got merged in.']]);
    });
});

describe('the reporting lag', () => {
    it('depends on how the VP joined Zoom, and can be overridden', () => {
        delete process.env.ASR_ROSTER_LAG_SECONDS;
        assert.equal(rosterLagSeconds('ZOOM', 'sdk'), 2.0);
        assert.equal(rosterLagSeconds('Zoom', 'dom'), 0);
        assert.equal(rosterLagSeconds('Teams', 'dom'), 0.75);
        process.env.ASR_ROSTER_LAG_SECONDS = '1.25';
        assert.equal(rosterLagSeconds('ZOOM', 'dom'), 1.25);
    });
});

describe('wiring', () => {
    const scribe = readFileSync(
        new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'),
        'utf8',
    );

    it('names Transcribe rows from the roster timeline before either engine is chosen', () => {
        const start = scribe.indexOf('async startTranscription');
        const wired = scribe.indexOf('kinesisStreamManager.setRosterLookup((start, end) => this.rosterNameFor(start, end))', start);
        const engine = scribe.indexOf('resolveVpAsrEngine(switches)', start);
        assert.ok(start !== -1 && wired > start && wired < engine);
    });

    it('stamps every participant change on the roster clock', () => {
        assert.match(scribe, /this\.rosterTimeline\.record\(this\.rosterClockSeconds\(\), speaker\)/);
    });
});
