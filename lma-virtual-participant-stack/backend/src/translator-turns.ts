/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A fresh Nova Sonic session for every translated turn.
 *
 * Within one session, Nova Sonic's translation drifts: after the first few
 * turns it starts transcribing English speech as Spanish and "translating" that
 * back into English, so the meeting hears its own words repeated. A new session
 * translates its first turn reliably. Replaying synthesized meetings against
 * Nova 2 Sonic with the translator prompt, a single session translated 25 of 40
 * utterances into the right language (5 of 8 in every one of 5 runs), and a
 * session per turn translated 64 of 64 (8 of 8 in 8 runs).
 *
 * So, in translator mode, when the current session starts answering a
 * participant's turn, the meeting's audio moves to a new session and the
 * current one is left to finish its answer:
 *
 *   - The new session is opened at that moment and so connects while the
 *     current one is still speaking. Audio for it is queued from the start and
 *     delivered as soon as its stream is up, so nothing is lost while it connects.
 *   - The audio heard since the participant's turn ended is replayed into the
 *     new session first ({@link RecentAudio}). That is where the next speaker's
 *     first words are when they start straight after the last one.
 *   - The outgoing session hears silence from then on, so a new speaker does not
 *     cut its answer off, and it is closed once its final transcript of the
 *     answer has arrived. That final text is what the meeting transcript gets.
 *   - Audio the new session produces while the outgoing one is still producing
 *     its own is held back and played after it, so the two never interleave.
 */

/** How long an outgoing session is given to finish its answer before it is closed anyway. */
export const TRANSLATOR_DRAIN_MAX_MS = 30_000;

/** The most meeting audio replayed into a new session. */
export const TRANSLATOR_BACKLOG_MAX_MS = 3_000;

/** Meeting audio as sent to the model, kept for a short while. */
export class RecentAudio {
    private chunks: Array<{ at: number; chunk: Buffer }> = [];

    constructor(private readonly keepMs: number = TRANSLATOR_BACKLOG_MAX_MS) {}

    push(chunk: Buffer, at: number = Date.now()): void {
        this.chunks.push({ at, chunk });
        while (this.chunks.length && this.chunks[0].at < at - this.keepMs) {
            this.chunks.shift();
        }
    }

    /** The chunks sent at or after `from`, oldest first. */
    since(from: number): Buffer[] {
        return this.chunks.filter((c) => c.at >= from).map((c) => c.chunk);
    }

    clear(): void {
        this.chunks = [];
    }
}

/** Milliseconds of 16 kHz, 16-bit mono PCM in `chunks`. */
export function pcmMs(chunks: Buffer[]): number {
    return Math.round(chunks.reduce((n, c) => n + c.length, 0) / 32);
}
