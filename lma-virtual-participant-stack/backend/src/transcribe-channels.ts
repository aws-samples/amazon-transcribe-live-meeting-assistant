/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Two-channel Amazon Transcribe input for a Virtual Participant with a voice
 * assistant.
 *
 * The participants and the assistant are kept on separate channels of ONE
 * stream, with channel identification on, so Transcribe never merges a
 * participant's sentence and the assistant's reply into one row:
 *
 *   ch_0  meeting_audio.monitor  participants  -> CALLER rows, roster names,
 *                                                 wake phrases, stop requests
 *   ch_1  agent_output.monitor   the assistant -> AGENT rows under the VP's name
 *
 * These are the same channel ids and the same CALLER/AGENT mapping as Stream
 * Audio's two-channel stream (lma-websocket-transcriber-stack, transcribe.ts),
 * and the same AGENT row shape as the MicroVM ASR engine's assistant session
 * (scribe.ts, handleAgentAsrSegment).
 *
 * One two-channel stream is billed for the stream's duration, like a mono one,
 * so the separation does not open a second stream.
 */
import type { TranscriptSegment } from './kinesis-stream.js';

/** Transcribe's id for the participants' channel (left). */
export const MEETING_CHANNEL_ID = 'ch_0';
/** Transcribe's id for the assistant's channel (right). */
export const ASSISTANT_CHANNEL_ID = 'ch_1';

/** 16 kHz, 16-bit mono: the format of every sink in the VP's audio graph. */
const BYTES_PER_SAMPLE = 2;
const SAMPLE_RATE = 16000;

/** Which source a Transcribe result came from. A mono stream only has participants. */
export function resultChannel(result: any, stereo: boolean): 'meeting' | 'assistant' {
    if (!stereo) {
        return 'meeting';
    }
    // Results without a ChannelId are treated as ch_0, as the transcriber stack does.
    return result?.ChannelId === ASSISTANT_CHANNEL_ID ? 'assistant' : 'meeting';
}

/**
 * Whether results on the assistant's channel are written to the transcript.
 *
 * Not in translator mode. There the assistant speaks the other language into a
 * stream opened in the meeting's language, so its channel comes back as a
 * garbled rendering; the provider publishes the exact text of each turn
 * instead (translator-transcript.ts, publishAssistantTurn), and writing both
 * would give the translation two rows. Read per result, so a mode change
 * mid-meeting takes effect on the next result.
 */
export function writesAssistantChannel(meetingMode: string | undefined): boolean {
    return meetingMode !== 'translator';
}

/** True when `name` is one of the VP's own identities in the meeting roster. */
export function isOwnIdentity(name: string, ownIdentities: string[]): boolean {
    const trimmed = (name || '').trim();
    if (!trimmed) {
        return false;
    }
    return ownIdentities.some((own) => (own || '').trim() === trimmed);
}

/**
 * The AGENT row for one result on the assistant's channel, or null if it has
 * no text.
 *
 * The id is keyed on the session offset and Transcribe's ResultId, so a
 * partial and its final update one row, and a reconnect (which restarts the
 * ids and the clock) cannot reuse a row from an earlier session. Times are
 * moved onto the meeting timeline by the same offset as the participants'.
 */
export function assistantSegmentFromResult(
    result: any,
    timeOffsetSeconds: number,
    speaker: string,
): TranscriptSegment | null {
    const alternative = result?.Alternatives?.[0];
    const transcript = (alternative?.Transcript || '').trim();
    if (!transcript) {
        return null;
    }
    const items: any[] = alternative?.Items ?? [];
    const timed = items.filter((item) => typeof item?.StartTime === 'number');
    const start = result?.StartTime ?? timed[0]?.StartTime ?? 0;
    const end = Math.max(result?.EndTime ?? timed[timed.length - 1]?.EndTime ?? start, start);
    const id = result?.ResultId ?? `t${start}`;
    return {
        channel: 'AGENT',
        segmentId: `vp-assistant-${timeOffsetSeconds}-${id}`,
        startTime: start + timeOffsetSeconds,
        endTime: end + timeOffsetSeconds,
        transcript,
        isPartial: Boolean(result?.IsPartial),
        speaker,
    };
}

/**
 * Interleaves two mono s16le captures into one stereo s16le stream.
 *
 * The two captures run on the same PulseAudio clock, so in steady state they
 * deliver at the same rate and every sample of one is paired with the sample
 * of the other that arrived alongside it. Output is always whole stereo
 * frames, whatever byte counts the pipes hand over.
 *
 * A side that stops delivering (a capture that died, or one that has not
 * started yet) must not hold up the other, or the participants' transcript
 * would stall behind a silent assistant. Once one side is more than
 * `maxSkewMs` ahead, its excess is sent paired with silence. Nothing is ever
 * dropped: the cost of a stall is at most `maxSkewMs` of added latency, and
 * the channels stay aligned to within that bound.
 *
 * A lead under the bound is not given back. If one capture starts delivering
 * 300 ms after the other, the earlier side reaches Transcribe 300 ms late for
 * the rest of the session, and the two channels' rows are offset by that much
 * on the meeting timeline. The padding counters below, logged when a session
 * ends, show how much silence was inserted on each side.
 */
export class StereoInterleaver {
    private left: Buffer = Buffer.alloc(0);
    private right: Buffer = Buffer.alloc(0);
    private readonly maxSkewSamples: number;
    /** Samples of silence inserted on each side, for diagnostics. */
    paddedLeftSamples = 0;
    paddedRightSamples = 0;

    constructor(maxSkewMs = 500) {
        this.maxSkewSamples = Math.max(0, Math.floor((SAMPLE_RATE * maxSkewMs) / 1000));
    }

    /** Add participants' audio (ch_0); returns the stereo bytes now ready. */
    pushLeft(chunk: Buffer): Buffer {
        this.left = this.left.length ? Buffer.concat([this.left, chunk]) : chunk;
        return this.drain();
    }

    /** Add the assistant's audio (ch_1); returns the stereo bytes now ready. */
    pushRight(chunk: Buffer): Buffer {
        this.right = this.right.length ? Buffer.concat([this.right, chunk]) : chunk;
        return this.drain();
    }

    private drain(): Buffer {
        const leftSamples = Math.floor(this.left.length / BYTES_PER_SAMPLE);
        const rightSamples = Math.floor(this.right.length / BYTES_PER_SAMPLE);
        const count = Math.max(
            Math.min(leftSamples, rightSamples),
            leftSamples - this.maxSkewSamples,
            rightSamples - this.maxSkewSamples,
        );
        if (count <= 0) {
            return Buffer.alloc(0);
        }
        const out = Buffer.alloc(count * 2 * BYTES_PER_SAMPLE);
        for (let i = 0; i < count; i += 1) {
            const l = i < leftSamples ? this.left.readInt16LE(i * BYTES_PER_SAMPLE) : 0;
            const r = i < rightSamples ? this.right.readInt16LE(i * BYTES_PER_SAMPLE) : 0;
            out.writeInt16LE(l, i * 4);
            out.writeInt16LE(r, i * 4 + 2);
        }
        const usedLeft = Math.min(count, leftSamples);
        const usedRight = Math.min(count, rightSamples);
        this.paddedLeftSamples += count - usedLeft;
        this.paddedRightSamples += count - usedRight;
        this.left = this.left.subarray(usedLeft * BYTES_PER_SAMPLE);
        this.right = this.right.subarray(usedRight * BYTES_PER_SAMPLE);
        return out;
    }
}

/**
 * A push-to-pull bridge from capture callbacks to the async iterable the
 * Transcribe request reads its audio from. Empty pushes are ignored; after
 * end() the iterator finishes once what was queued has been read.
 */
export class PcmQueue implements AsyncIterable<Buffer> {
    private readonly chunks: Buffer[] = [];
    private waiter: ((chunk: Buffer | null) => void) | null = null;
    private ended = false;

    push(chunk: Buffer): void {
        if (this.ended || chunk.length === 0) {
            return;
        }
        const waiter = this.waiter;
        if (waiter) {
            this.waiter = null;
            waiter(chunk);
        } else {
            this.chunks.push(chunk);
        }
    }

    end(): void {
        this.ended = true;
        const waiter = this.waiter;
        if (waiter) {
            this.waiter = null;
            waiter(null);
        }
    }

    async *[Symbol.asyncIterator](): AsyncIterator<Buffer> {
        for (;;) {
            const queued = this.chunks.shift();
            if (queued) {
                yield queued;
                continue;
            }
            if (this.ended) {
                return;
            }
            const next = await new Promise<Buffer | null>((resolve) => {
                this.waiter = resolve;
            });
            if (next === null) {
                return;
            }
            yield next;
        }
    }
}
