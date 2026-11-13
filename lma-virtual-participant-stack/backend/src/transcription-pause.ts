/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Records on the meeting record that transcription was paused or resumed with
 * the meeting chat commands (`LMA pause` / `LMA start`), so the meeting page
 * can say why the transcript has stopped, since when, and who paused it.
 *
 * Called by every platform handler right after it flips `details.start` for
 * one of those two commands, and only there: consent withholding capture
 * (consent-runtime.ts) and the end of the meeting also turn `details.start`
 * off, and the meeting page explains those in its own way.
 *
 * Best effort. It never throws and never rejects, so a handler can call it
 * without awaiting it; a lost record costs the banner, never the meeting.
 */
import { kinesisStreamManager } from './kinesis-stream.js';
import { createLogger } from './logger.js';

const log = createLogger('transcription-pause');

/** Longest sender name stored; a chat display name is far shorter. */
export const MAX_PAUSED_BY_LENGTH = 200;

/** What the record is written through: the meeting's Kinesis writer. */
export interface TranscriptionPauseSink {
    /** Whether the meeting record exists yet (START was sent). */
    readonly meetingStarted: boolean;
    sendTranscriptionPause(paused: boolean, changedAt: string, pausedBy: string | null): Promise<void>;
}

/** The sender's display name as stored: trimmed and bounded, or null when there is none. */
export const pausedByName = (sender: string | null | undefined): string | null => {
    const name = typeof sender === 'string' ? sender.trim() : '';
    return name ? name.slice(0, MAX_PAUSED_BY_LENGTH) : null;
};

/**
 * Records a pause (`paused` true, with the chat sender when known) or a resume
 * (`paused` false). Nothing is written before the meeting record exists: there
 * is no record to put it on yet.
 */
export async function recordTranscriptionPause(
    paused: boolean,
    sender: string | null,
    sink: TranscriptionPauseSink = kinesisStreamManager,
    now: () => Date = () => new Date(),
): Promise<void> {
    try {
        if (!sink.meetingStarted) {
            log.info(`Transcription ${paused ? 'pause' : 'resume'} not recorded: the meeting record does not exist yet`);
            return;
        }
        await sink.sendTranscriptionPause(paused, now().toISOString(), paused ? pausedByName(sender) : null);
    } catch (error) {
        log.warn(`Transcription ${paused ? 'pause' : 'resume'} could not be recorded (non-fatal):`, error);
    }
}
