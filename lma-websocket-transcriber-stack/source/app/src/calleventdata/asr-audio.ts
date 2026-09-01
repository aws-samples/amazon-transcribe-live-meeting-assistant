/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Audio plumbing for the MicroVM ASR engine.
 *
 * Clients send one interleaved 2-channel 16-bit PCM stream at whatever rate they
 * capture at (48 kHz from a browser AudioContext by default). The ASR engine
 * takes one mono 16 kHz stream per session, so each channel is split out and
 * resampled here. Both steps are pure and stateful-per-call, never per frame.
 */
import { appendSpeakerLabel, formatSpeakerLabel } from './diarization';

export const ASR_SAMPLE_RATE = 16000;

const BYTES_PER_SAMPLE = 2;
const STEREO_FRAME_BYTES = BYTES_PER_SAMPLE * 2;
const INT16_MIN = -32768;
const INT16_MAX = 32767;

/** ch_0 is the browser tab / meeting audio (CALLER), ch_1 the microphone (AGENT). */
export const ASR_CHANNEL_IDS = ['ch_0', 'ch_1'] as const;
export type AsrChannelId = (typeof ASR_CHANNEL_IDS)[number];

export const CHANNEL_INDEX: Record<AsrChannelId, number> = { ch_0: 0, ch_1: 1 };

export const channelToTranscriptChannel = (channelId: AsrChannelId): string =>
    channelId === 'ch_0' ? 'CALLER' : 'AGENT';

/**
 * Splits the interleaved stereo stream into per-channel mono buffers.
 *
 * Holds back a trailing partial frame so a chunk boundary that lands mid-frame
 * cannot swap the two channels for the rest of the meeting.
 */
export class StereoDeinterleaver {
    private leftover: Buffer = Buffer.alloc(0);

    split(data: Uint8Array): Record<AsrChannelId, Buffer> {
        const incoming = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
        const source =
            this.leftover.length > 0 ? Buffer.concat([this.leftover, incoming]) : incoming;
        const frames = Math.floor(source.length / STEREO_FRAME_BYTES);
        const consumed = frames * STEREO_FRAME_BYTES;
        this.leftover = Buffer.from(source.subarray(consumed));

        const ch0 = Buffer.alloc(frames * BYTES_PER_SAMPLE);
        const ch1 = Buffer.alloc(frames * BYTES_PER_SAMPLE);
        for (let frame = 0; frame < frames; frame += 1) {
            const offset = frame * STEREO_FRAME_BYTES;
            ch0.writeInt16LE(source.readInt16LE(offset), frame * BYTES_PER_SAMPLE);
            ch1.writeInt16LE(
                source.readInt16LE(offset + BYTES_PER_SAMPLE),
                frame * BYTES_PER_SAMPLE
            );
        }
        return { ch_0: ch0, ch_1: ch1 };
    }
}

const hammingSinc = (taps: number, normalizedCutoff: number): Float32Array => {
    const coefficients = new Float32Array(taps);
    const middle = (taps - 1) / 2;
    let sum = 0;
    for (let i = 0; i < taps; i += 1) {
        const n = i - middle;
        const sinc =
            n === 0 ? 2 * normalizedCutoff : Math.sin(2 * Math.PI * normalizedCutoff * n) / (Math.PI * n);
        const window = 0.54 - 0.46 * Math.cos((2 * Math.PI * i) / (taps - 1));
        coefficients[i] = sinc * window;
        sum += coefficients[i];
    }
    for (let i = 0; i < taps; i += 1) {
        coefficients[i] /= sum;
    }
    return coefficients;
};

/**
 * Streaming rate converter for one mono 16-bit channel.
 *
 * Downsampling low-passes first (a bare decimation would alias speech energy
 * above 8 kHz back into the band the model reads), then reads at fractional
 * positions with linear interpolation, which handles non-integer ratios such as
 * 44.1 kHz the same way as 48 kHz. Filter history and the fractional phase carry
 * across chunks, so a stream resampled in pieces matches one resampled whole.
 */
export class ChannelResampler {
    private readonly step: number;

    private readonly passthrough: boolean;

    private readonly coefficients: Float32Array | null;

    private history: Float32Array;

    private previous = 0;

    private phase = 1;

    constructor(
        private readonly inputRate: number,
        private readonly outputRate: number = ASR_SAMPLE_RATE,
        taps = 33
    ) {
        if (!Number.isFinite(inputRate) || inputRate <= 0) {
            throw new Error(`invalid input sample rate: ${inputRate}`);
        }
        this.step = inputRate / outputRate;
        this.passthrough = inputRate === outputRate;
        const needsLowPass = inputRate > outputRate;
        this.coefficients = needsLowPass
            ? hammingSinc(taps, (0.45 * outputRate) / inputRate)
            : null;
        this.history = new Float32Array(this.coefficients ? taps - 1 : 0);
    }

    get needsResampling(): boolean {
        return !this.passthrough;
    }

    process(monoPcm: Buffer): Buffer {
        if (this.passthrough) {
            return monoPcm;
        }
        const inputCount = Math.floor(monoPcm.length / BYTES_PER_SAMPLE);
        if (inputCount === 0) {
            return Buffer.alloc(0);
        }

        const filtered = this.lowPass(monoPcm, inputCount);

        // index 0 is the last sample of the previous chunk, so an output sample
        // that falls between chunks is still interpolated from both neighbours.
        const source = new Float32Array(filtered.length + 1);
        source[0] = this.previous;
        source.set(filtered, 1);

        const estimated = Math.ceil(filtered.length / this.step) + 2;
        const out = Buffer.alloc(estimated * BYTES_PER_SAMPLE);
        let written = 0;
        let position = this.phase;
        while (Math.floor(position) + 1 <= filtered.length) {
            const index = Math.floor(position);
            const fraction = position - index;
            const sample = source[index] * (1 - fraction) + source[index + 1] * fraction;
            out.writeInt16LE(
                Math.max(INT16_MIN, Math.min(INT16_MAX, Math.round(sample))),
                written * BYTES_PER_SAMPLE
            );
            written += 1;
            position += this.step;
        }

        this.phase = position - filtered.length;
        this.previous = filtered[filtered.length - 1];
        return out.subarray(0, written * BYTES_PER_SAMPLE);
    }

    private lowPass(monoPcm: Buffer, inputCount: number): Float32Array {
        const samples = new Float32Array(inputCount);
        for (let i = 0; i < inputCount; i += 1) {
            samples[i] = monoPcm.readInt16LE(i * BYTES_PER_SAMPLE);
        }
        if (!this.coefficients) {
            return samples;
        }

        const taps = this.coefficients.length;
        const padded = new Float32Array(this.history.length + inputCount);
        padded.set(this.history, 0);
        padded.set(samples, this.history.length);

        const filtered = new Float32Array(inputCount);
        for (let i = 0; i < inputCount; i += 1) {
            let accumulator = 0;
            for (let k = 0; k < taps; k += 1) {
                accumulator += this.coefficients[k] * padded[i + taps - 1 - k];
            }
            filtered[i] = accumulator;
        }

        this.history = padded.slice(padded.length - (taps - 1));
        return filtered;
    }
}

/**
 * Per-meeting speaker display names.
 *
 * The engine reports per-channel, per-session ids (spk_0, spk_1, ...). Each is
 * appended to the channel's own name through the SAME formatter the Amazon
 * Transcribe path uses, so a transcript reads identically whichever engine produced
 * it: "Other Participant (spk_0)", "alex@example.com (spk_1)".
 *
 * The suffix is what does the work. Both channels run independent sessions and both
 * restart at spk_0 for DIFFERENT people, so the base name separates the channels
 * while the suffix separates voices within one. Without it the first voice heard on
 * a channel took that channel's placeholder name alone, and a reviewer reading such
 * a transcript concluded two correctly separated speakers had been merged.
 *
 * Stateless: it holds no counter, because nothing needs renumbering.
 */
export class SpeakerNameRegistry {
    /**
     * @param channelId which audio channel produced the utterance
     * @param speakerId engine speaker id, or undefined when diarization is off
     * @param channelName the name LMA already has for this channel's participant
     */
    nameFor(
        channelId: AsrChannelId,
        speakerId: string | null | undefined,
        channelName: string
    ): string {
        return appendSpeakerLabel(channelName, formatSpeakerLabel(speakerId ?? undefined));
    }
}
