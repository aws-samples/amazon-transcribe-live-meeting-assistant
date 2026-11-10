/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Audio frames that arrive while the START before them is still being handled.
 *
 * A client sends START and then its audio straight away: on a reconnect, the
 * audio it held while disconnected goes out in the same tick as the START, and
 * on any connection the live audio follows without waiting for a reply (the
 * protocol has none). The socket's message handler does not wait for one frame
 * before handling the next, and START reads the settings, checks consent and
 * writes the meeting's start event (or resumes a held session) before the
 * connection carries a session, so audio right behind it would find none.
 * Frames for a connection whose START is in progress are held here and handed
 * back, in the order they arrived, once the START has a session for them.
 *
 * The hold is bounded by bytes per connection. When it is full the oldest
 * frames are dropped, and how much was dropped is reported on release.
 */

/** Seconds of audio a connection's hold keeps: the 3 s a client replays after a reconnect, plus the START's own handling. */
export const PENDING_AUDIO_SECONDS = 8;

// The recording is always two channels of 16-bit PCM (see utils/wav.ts).
const BYTES_PER_FRAME = 2 * 2;
const DEFAULT_SAMPLING_RATE = 16000;
const MIN_SAMPLING_RATE = 8000;
const MAX_SAMPLING_RATE = 48000;

/**
 * The byte bound for a START's hold at the meeting's sampling rate. A missing
 * or out-of-range rate is clamped, so a START cannot ask for a larger hold
 * than 48 kHz stereo allows.
 */
export const pendingAudioLimitBytes = (samplingRate: number | undefined): number => {
    const rate = Number.isFinite(samplingRate) && (samplingRate as number) > 0 ? (samplingRate as number) : DEFAULT_SAMPLING_RATE;
    const clamped = Math.min(Math.max(rate, MIN_SAMPLING_RATE), MAX_SAMPLING_RATE);
    return Math.round(clamped * BYTES_PER_FRAME * PENDING_AUDIO_SECONDS);
};

/** What a START's hold returned when it finished. */
export interface ReleasedAudio {
    /** The frames kept, in the order they arrived. */
    frames: Uint8Array[];
    bytes: number;
    /** The oldest frames that did not fit and were dropped. */
    droppedFrames: number;
    droppedBytes: number;
}

interface Hold {
    limitBytes: number;
    frames: Uint8Array[];
    bytes: number;
    droppedFrames: number;
    droppedBytes: number;
}

export class PendingAudioFrames<K> {
    private readonly pending = new Map<K, Hold>();

    /** A START began on this connection: hold its audio until it has finished. */
    begin(key: K, limitBytes: number): void {
        this.pending.set(key, { limitBytes, frames: [], bytes: 0, droppedFrames: 0, droppedBytes: 0 });
    }

    /** Whether a START is in progress on this connection. */
    holding(key: K): boolean {
        return this.pending.has(key);
    }

    /**
     * Hold a frame for a START in progress on this connection, dropping the
     * oldest held frames beyond the bound. False when no START is in progress.
     */
    hold(key: K, frame: Uint8Array): boolean {
        const hold = this.pending.get(key);
        if (!hold) {
            return false;
        }
        hold.frames.push(frame);
        hold.bytes += frame.length;
        while (hold.bytes > hold.limitBytes && hold.frames.length > 0) {
            const oldest = hold.frames.shift() as Uint8Array;
            hold.bytes -= oldest.length;
            hold.droppedFrames += 1;
            hold.droppedBytes += oldest.length;
        }
        return true;
    }

    /** The START finished: the frames held for it, in the order they arrived. */
    release(key: K): ReleasedAudio {
        const hold = this.pending.get(key);
        this.pending.delete(key);
        if (!hold) {
            return { frames: [], bytes: 0, droppedFrames: 0, droppedBytes: 0 };
        }
        const { frames, bytes, droppedFrames, droppedBytes } = hold;
        return { frames, bytes, droppedFrames, droppedBytes };
    }

    /** The START was refused or the connection closed: forget its frames. Returns the bytes discarded. */
    drop(key: K): number {
        const bytes = this.pending.get(key)?.bytes ?? 0;
        this.pending.delete(key);
        return bytes;
    }

    /** Connections with a START in progress. */
    get size(): number {
        return this.pending.size;
    }
}
