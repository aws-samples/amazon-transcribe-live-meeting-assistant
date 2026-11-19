/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The end-of-meeting work both shutdown paths share (meeting-teardown.ts),
 * driven with stand-in steps that record the order they ran in.
 */

import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    EVIDENCE_WAIT_MS,
    MeetingTeardown,
    STOP_CONSENT_VIDEO_WAIT_MS,
    STOP_EVIDENCE_WAIT_MS,
    type MeetingTeardownSteps,
} from './meeting-teardown.js';
import { LEAVE_BUDGET_MS } from './meeting-leave.js';
import { meetingOver, resetMeetingPhase } from './meeting-phase.js';
import { TERMINATE_WAIT_MS } from './microvm-supervisor.js';

const never = (): Promise<never> => new Promise<never>(() => {});

function recorder(overrides: Partial<MeetingTeardownSteps> = {}): { steps: MeetingTeardownSteps; ran: string[] } {
    const ran: string[] = [];
    const steps: MeetingTeardownSteps = {
        stopPageMonitors: () => {
            ran.push('monitors');
        },
        stopConsent: () => {
            ran.push('consent');
        },
        consentEvidenceSettled: async () => {
            ran.push('evidence');
        },
        consentVideoSettled: async () => {
            ran.push('consent-video');
        },
        leaveMeeting: async (why) => {
            ran.push(`leave:${why}`);
        },
        stopKeyframes: async () => {
            ran.push('keyframes');
        },
        stopTranscription: async () => {
            ran.push('transcription');
        },
        finishAudioRecording: async () => {
            ran.push('audio');
            return 's3://bucket/audio.wav';
        },
        sendRecordingUrl: async (url) => {
            ran.push(`recording-url:${url}`);
        },
        finishVideoRecording: async () => {
            ran.push('video');
            return 's3://bucket/video.mp4';
        },
        sendVideoRecordingUrl: async (url) => {
            ran.push(`video-url:${url}`);
        },
        sendEnd: async () => {
            ran.push('end');
        },
        ...overrides,
    };
    return { steps, ran };
}

const fast = { evidenceMs: 20, stopEvidenceMs: 20, stopConsentVideoMs: 20 };

test('after a stop signal: leave first, then the recordings and their links, then END', async () => {
    const { steps, ran } = recorder();
    await new MeetingTeardown(steps, fast).run({ stopping: true, leaveReason: 'stopped by SIGTERM' });
    assert.deepEqual(ran, [
        'leave:stopped by SIGTERM',
        'monitors',
        'consent',
        'evidence',
        'consent-video',
        'keyframes',
        'transcription',
        'audio',
        'recording-url:s3://bucket/audio.wav',
        'video',
        'video-url:s3://bucket/video.mp4',
        'end',
    ]);
});

test('at a normal end: the consent flow first, then the leave, then the same steps', async () => {
    const { steps, ran } = recorder();
    await new MeetingTeardown(steps, fast).run({ stopping: false, leaveReason: 'ended: end-command' });
    assert.deepEqual(ran.slice(0, 5), ['monitors', 'consent', 'evidence', 'consent-video', 'leave:ended: end-command']);
    assert.deepEqual(ran.slice(-3), ['video', 'video-url:s3://bucket/video.mp4', 'end']);
});

test('no leave when the VP is no longer in the meeting', async () => {
    const { steps, ran } = recorder();
    await new MeetingTeardown(steps, fast).run({ stopping: false, leaveReason: null });
    assert.ok(!ran.some((r) => r.startsWith('leave')));
    assert.equal(ran.at(-1), 'end');
});

test('no link is sent for a recording that produced no file', async () => {
    const { steps, ran } = recorder({
        finishAudioRecording: async () => null,
        finishVideoRecording: async () => undefined,
    });
    await new MeetingTeardown(steps, fast).run({ stopping: true, leaveReason: 'stopped by SIGTERM' });
    assert.ok(!ran.some((r) => r.startsWith('recording-url') || r.startsWith('video-url')));
    assert.equal(ran.at(-1), 'end');
});

test('a failing step does not skip the ones after it', async () => {
    const { steps, ran } = recorder({
        leaveMeeting: async () => {
            throw new Error('page gone');
        },
        finishAudioRecording: async () => {
            throw new Error('upload failed');
        },
    });
    await new MeetingTeardown(steps, fast).run({ stopping: true, leaveReason: 'stopped by SIGTERM' });
    assert.deepEqual(ran.slice(-3), ['video', 'video-url:s3://bucket/video.mp4', 'end']);
});

test('runs once: a second caller waits for the run in progress', async () => {
    let releaseEvidence: () => void = () => {};
    const { steps, ran } = recorder({
        consentEvidenceSettled: () => new Promise<void>((resolve) => (releaseEvidence = resolve)),
    });
    const teardown = new MeetingTeardown(steps, { ...fast, evidenceMs: 5_000 });
    const first = teardown.run({ stopping: false, leaveReason: null });
    assert.equal(teardown.started, true);
    const second = teardown.run({ stopping: true, leaveReason: 'stopped by SIGTERM' });
    assert.equal(first, second);
    releaseEvidence();
    await second;
    assert.equal(ran.filter((r) => r === 'end').length, 1);
    assert.ok(!ran.some((r) => r.startsWith('leave')), 'the first run decides the steps');
});

test('after a stop signal the consent waits are short, and an unfinished consent video stop is left to the consent flow', async () => {
    const { steps, ran } = recorder({ consentEvidenceSettled: never, consentVideoSettled: never });
    const started = Date.now();
    await new MeetingTeardown(steps, fast).run({ stopping: true, leaveReason: 'stopped by SIGTERM' });
    assert.ok(Date.now() - started < 1_000);
    assert.ok(!ran.includes('video'), 'the video recording is not stopped a second time');
    assert.ok(ran.includes('recording-url:s3://bucket/audio.wav'));
    assert.equal(ran.at(-1), 'end');
});

test("the stop signal's fixed waits leave room in the MicroVM terminate wait", () => {
    assert.ok(STOP_EVIDENCE_WAIT_MS < EVIDENCE_WAIT_MS);
    // Leave, evidence and consent video are the bounded waits ahead of the
    // uploads; together they must leave most of the terminate wait to them.
    assert.ok(LEAVE_BUDGET_MS + STOP_EVIDENCE_WAIT_MS + STOP_CONSENT_VIDEO_WAIT_MS <= TERMINATE_WAIT_MS * 0.7);
});

test('the meeting is over for the VP from the moment its teardown begins, so the dialog watchdog stands down', async () => {
    resetMeetingPhase();
    let overAtFirstStep: boolean | null = null;
    const { steps } = recorder({
        stopPageMonitors: () => {
            overAtFirstStep = meetingOver();
        },
    });
    assert.equal(meetingOver(), false);
    await new MeetingTeardown(steps, fast).run({ stopping: true, leaveReason: 'stopped by SIGTERM' });
    assert.equal(overAtFirstStep, true);
    resetMeetingPhase();
});
