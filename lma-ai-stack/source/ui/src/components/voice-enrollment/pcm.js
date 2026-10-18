/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Turn a recorded clip into the audio the enrollment backend expects.
 *
 * The backend streams these to an ASR session in `embed` mode, which wants
 * 16 kHz mono 16-bit PCM. A browser's MediaRecorder produces compressed audio at
 * the device's own rate, so each clip is decoded, mixed to mono, resampled and
 * quantised here.
 *
 * Done per clip rather than as a stream because enrollment is a handful of short
 * recordings, not a meeting: there is no latency requirement, and decoding a
 * finished clip is far simpler than an AudioWorklet pipeline.
 *
 * None of this audio is uploaded anywhere except the one request that computes
 * the voiceprint, and the backend discards it when that request returns.
 */

/** What the ASR embedding session reads. */
export const TARGET_SAMPLE_RATE = 16000;

/**
 * Mix every channel down to one.
 *
 * Averaged rather than taking the first channel: a laptop with a stereo
 * microphone can put most of a voice on one side, and keeping only the other
 * would quietly halve the signal.
 */
export const toMono = (channels, length) => {
  const mono = new Float32Array(length);
  for (let channel = 0; channel < channels.length; channel += 1) {
    const samples = channels[channel];
    for (let i = 0; i < length; i += 1) {
      mono[i] += samples[i] / channels.length;
    }
  }
  return mono;
};

/**
 * Resample by linear interpolation.
 *
 * Not the most faithful method available, but a speaker embedding is derived
 * from spectral envelope rather than fine detail, and the alternative -- pulling
 * in a resampling library for a few seconds of speech -- is not worth the
 * bundle. Downsampling without a low-pass filter does alias; at 48 kHz to 16 kHz
 * the aliased energy lands above the vocal range that carries identity.
 */
export const resample = (samples, fromRate, toRate) => {
  if (fromRate === toRate) {
    return samples;
  }
  const ratio = fromRate / toRate;
  const length = Math.floor(samples.length / ratio);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const index = Math.floor(position);
    const next = Math.min(index + 1, samples.length - 1);
    const fraction = position - index;
    out[i] = samples[index] * (1 - fraction) + samples[next] * fraction;
  }
  return out;
};

/**
 * Float samples in [-1, 1) to little-endian 16-bit PCM.
 *
 * Clamped before scaling: a sample slightly outside the range is normal after
 * resampling, and letting it wrap would put a loud click in the middle of a
 * recording somebody is about to be identified by.
 */
export const toPcm16 = (samples) => {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, Math.round(clamped * 32767), true);
  }
  return buffer;
};

/** Base64, in chunks, so a long clip does not overflow the argument list. */
export const toBase64 = (buffer) => {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
};

/** Seconds of audio in a decoded buffer. */
export const durationOf = (audioBuffer) => audioBuffer.length / audioBuffer.sampleRate;

/**
 * A recorded clip as base64 16 kHz mono PCM, plus how long it is.
 *
 * `decode` is injected so the conversion can be tested without a browser.
 */
export const clipToPcm = async (blob, decode) => {
  const arrayBuffer = await blob.arrayBuffer();
  const audioBuffer = await decode(arrayBuffer);
  const channels = [];
  for (let channel = 0; channel < audioBuffer.numberOfChannels; channel += 1) {
    channels.push(audioBuffer.getChannelData(channel));
  }
  const mono = toMono(channels, audioBuffer.length);
  const resampled = resample(mono, audioBuffer.sampleRate, TARGET_SAMPLE_RATE);
  return {
    pcm: toBase64(toPcm16(resampled)),
    seconds: resampled.length / TARGET_SAMPLE_RATE,
  };
};
