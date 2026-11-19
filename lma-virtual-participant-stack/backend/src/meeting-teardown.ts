/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The end-of-meeting work, shared by the two ways a Virtual Participant shuts
 * down: main()'s cleanup when the meeting ends on its own, and the stop-signal
 * handler when it is ended from LMA (index.ts). Both run the same steps, so a
 * meeting ended from LMA gets its recording and video links and its END event
 * like any other.
 *
 * It runs once. A second caller (a stop signal arriving while main()'s cleanup
 * is under way) waits for the run already in progress rather than repeating it.
 *
 * Every step is in its own try, so one failing does not skip the rest.
 *
 * Order, after a stop signal (`stopping`): leave the meeting first, while the
 * browser is still open; then the consent flow, with short waits; then key
 * frames, transcription, the audio recording and its link, the video recording
 * and its link, and END last. The waits are shortened because the whole
 * shutdown has to fit inside the stop grace period: ECS stops the task 30
 * seconds after SIGTERM, and the MicroVM terminate hook waits 25 seconds
 * (TERMINATE_WAIT_MS). Without a stop signal the consent flow goes first,
 * since its evidence may still be a screenshot of the meeting page, and the
 * leave follows it.
 */

import { noteMeetingOver } from './meeting-phase.js';
import { createLogger } from './logger.js';

const log = createLogger('meeting-teardown');

/** How long the consent flow's evidence may take to land, at a normal end. */
export const EVIDENCE_WAIT_MS = 15_000;
/** The same wait after a stop signal. */
export const STOP_EVIDENCE_WAIT_MS = 3_000;
/**
 * After a stop signal, how long to wait for a video stop the consent flow
 * began. At a normal end it is waited for in full.
 */
export const STOP_CONSENT_VIDEO_WAIT_MS = 5_000;

export interface MeetingTeardownSteps {
    /** Stop the timers that poll the page (audio diagnostics, sender bypass, device specs). */
    stopPageMonitors(): void;
    stopConsent(): void;
    consentEvidenceSettled(): Promise<unknown>;
    /** Settles once a video stop the consent flow began has finished. */
    consentVideoSettled(): Promise<unknown>;
    /** Leave on the platform; bounded by the caller. */
    leaveMeeting(why: string): Promise<void>;
    stopKeyframes(): Promise<void>;
    stopTranscription(): Promise<void>;
    /** Stop and upload the audio recording; its URL, if there is one. */
    finishAudioRecording(): Promise<string | null | undefined>;
    sendRecordingUrl(url: string): Promise<void>;
    /** Stop and upload the screen recording; its URL, if there is one. */
    finishVideoRecording(): Promise<string | null | undefined>;
    sendVideoRecordingUrl(url: string): Promise<void>;
    sendEnd(): Promise<void>;
}

export interface TeardownOptions {
    /** Shutting down on a stop signal: leave first, short waits. */
    stopping: boolean;
    /** Leave on the platform; null when the VP is no longer in the meeting. */
    leaveReason: string | null;
}

/** Resolves true when `promise` settles within `ms`, false otherwise. Never rejects. */
async function settlesWithin(promise: Promise<unknown>, ms: number): Promise<boolean> {
    let timer: NodeJS.Timeout | undefined;
    try {
        return await Promise.race([
            promise.then(
                () => true,
                () => true,
            ),
            new Promise<boolean>((resolve) => {
                timer = setTimeout(() => resolve(false), ms);
            }),
        ]);
    } finally {
        if (timer) clearTimeout(timer);
    }
}

export interface TeardownWaits {
    evidenceMs: number;
    stopEvidenceMs: number;
    stopConsentVideoMs: number;
}

const DEFAULT_WAITS: TeardownWaits = {
    evidenceMs: EVIDENCE_WAIT_MS,
    stopEvidenceMs: STOP_EVIDENCE_WAIT_MS,
    stopConsentVideoMs: STOP_CONSENT_VIDEO_WAIT_MS,
};

export class MeetingTeardown {
    private running: Promise<void> | null = null;

    constructor(
        private readonly steps: MeetingTeardownSteps,
        private readonly waits: TeardownWaits = DEFAULT_WAITS,
    ) {}

    /** True once a run has started. */
    get started(): boolean {
        return this.running !== null;
    }

    /** Run the teardown, once; a later call returns the first run. */
    run(options: TeardownOptions): Promise<void> {
        if (!this.running) {
            this.running = this.runSteps(options);
        } else {
            log.info('[teardown] already running; waiting for it');
        }
        return this.running;
    }

    private async step<T>(name: string, fn: () => Promise<T> | T): Promise<T | undefined> {
        try {
            return await fn();
        } catch (error) {
            log.error(`[teardown] ${name} failed:`, error);
            return undefined;
        }
    }

    private async runSteps({ stopping, leaveReason }: TeardownOptions): Promise<void> {
        const s = this.steps;
        log.info(`[teardown] start (${stopping ? 'stop signal' : 'meeting ended'})`);
        // From here nothing on the page needs a human (the dialog watchdog).
        noteMeetingOver();

        if (stopping && leaveReason) await this.step('leave', () => s.leaveMeeting(leaveReason));

        await this.step('page monitors', () => s.stopPageMonitors());

        await this.step('consent stop', () => s.stopConsent());
        await this.step('consent evidence', () =>
            settlesWithin(s.consentEvidenceSettled(), stopping ? this.waits.stopEvidenceMs : this.waits.evidenceMs),
        );
        // A video stop the consent flow began (after an objection) must finish
        // before the stop below, which would otherwise run over the same
        // recording. After a stop signal the wait is bounded, and if it has not
        // finished the video stop below is left to it.
        let consentVideoDone = true;
        if (stopping) {
            consentVideoDone =
                (await this.step('consent video', () =>
                    settlesWithin(s.consentVideoSettled(), this.waits.stopConsentVideoMs),
                )) ?? true;
        } else {
            await this.step('consent video', () => s.consentVideoSettled());
        }

        if (!stopping && leaveReason) await this.step('leave', () => s.leaveMeeting(leaveReason));

        // Before transcription, which closes the meeting's stream, so a frame
        // being captured is still announced.
        await this.step('key frames', () => s.stopKeyframes());
        await this.step('transcription', () => s.stopTranscription());

        const recordingUrl = await this.step('audio recording', () => s.finishAudioRecording());
        if (recordingUrl) {
            log.info(`Recording uploaded: ${recordingUrl}`);
            await this.step('recording link', () => s.sendRecordingUrl(recordingUrl));
        }

        if (consentVideoDone) {
            const videoUrl = await this.step('video recording', () => s.finishVideoRecording());
            if (videoUrl) {
                log.info(`Video recording uploaded: ${videoUrl}`);
                await this.step('video recording link', () => s.sendVideoRecordingUrl(videoUrl));
            }
        } else {
            log.warn('[teardown] the consent flow is still stopping the video recording; not stopping it again');
        }

        await this.step('END event', () => s.sendEnd());
        log.info('[teardown] done');
    }
}
