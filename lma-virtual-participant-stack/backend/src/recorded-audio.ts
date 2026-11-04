/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the meeting's audio recording gets for a chunk of captured audio.
 *
 * While capture is off (`LMA pause`, or before the meeting starts) the
 * transcription engines are still fed: Amazon Transcribe gets silence of the
 * same length, which keeps its stream open, and the MicroVM sessions are fed
 * silence so their clock keeps meeting time. The transcript's clock therefore
 * runs on through a pause. The recording gets the same silence, not nothing:
 * leaving the paused time out of the file would put every later transcript
 * line ahead of its audio by the length of the pause, and the meeting page's
 * player reads transcript times as positions in the audio recording.
 *
 * Silence carries nothing of what was said while paused.
 */
export function recordedAudio(chunk: Buffer, capturing: boolean): Buffer {
    return capturing ? chunk : Buffer.alloc(chunk.length);
}
