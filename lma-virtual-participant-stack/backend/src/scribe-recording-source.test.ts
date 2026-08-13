/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Regression tests for the S3 audio recording missing the voice assistant.
 *
 * Reported from a live meeting: "the audio recording did not contain the voice
 * assistant spoken responses, but the video recording does. Same thing with
 * Teams." Both were true, and the cause was which PulseAudio source each one is
 * fed from:
 *
 *   video recording   ffmpeg on combined_audio.monitor  -> meeting + agent  ✓
 *   audio recording   ffmpeg on meeting_audio.monitor   -> meeting only     ✗
 *
 * meeting_audio is deliberately agent-free so Nova never hears itself, so the
 * assistant's replies were structurally absent from every audio recording ever
 * produced — not intermittently lost. It went unnoticed because the assistant's
 * transcript comes from Nova's own text output, not from Transcribe, so the
 * transcript looked complete while the audio it supposedly came from was not.
 *
 * The recording is now teed inside audioStream(), from the same chunks that go to
 * Transcribe. These tests assert the wiring at the source level because the
 * alternative is spawning ffmpeg and PulseAudio.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

const src = readFileSync(
    new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'),
    'utf8',
);

/** Body of the named method, up to the start of the next method at the same depth. */
function methodBody(name: string): string {
    const start = src.indexOf(name);
    assert.notEqual(start, -1, `${name} not found in scribe.ts`);
    const rest = src.slice(start);
    // Methods in this class are declared at four-space indentation.
    const end = rest.slice(1).search(/\n {4}(private|public|async|\/\*\*)/);
    return end === -1 ? rest : rest.slice(0, end + 1);
}

test('the recording is written from the combined-audio stream', () => {
    // combined_audio.monitor is the only source carrying BOTH the meeting and the
    // agent, which is what a meeting recording has to contain.
    const body = methodBody('private async *audioStream');
    assert.match(body, /combined_audio\.monitor/);
    assert.match(body, /recordingStream\.write\(chunk\)/);
});

test('the recording is NOT written from the meeting-only stream', () => {
    // The defect itself. meeting_audio.monitor exists precisely to exclude the
    // agent, so anything recorded from it can never contain the assistant.
    const body = methodBody('private async writeAudio');
    assert.match(body, /meeting_audio\.monitor/, 'writeAudio should still feed Nova from meeting-only audio');
    assert.doesNotMatch(
        body,
        /recordingStream\.write/,
        'writeAudio must not write the recording — meeting_audio.monitor has no agent audio',
    );
});

test('writeAudio no longer takes a recording stream at all', () => {
    // Leaving the parameter in place would invite the bug straight back; removing
    // it makes reintroducing the write a compile error rather than a silent
    // regression that only shows up in a recording nobody plays back.
    assert.match(src, /private async writeAudio\(transcribeResponse: any\): Promise<void>/);
    assert.doesNotMatch(src, /this\.writeAudio\(response, recordingStream\)/);
});

test('the recording only captures audio once the meeting has started', () => {
    // audioStream runs before details.start to keep the Transcribe stream alive
    // with silence; recording that would prepend the pre-join period to every file.
    const body = methodBody('private async *audioStream');
    const guard = body.slice(0, body.indexOf('recordingStream.write(chunk)'));
    assert.match(guard.split('\n').slice(-4).join('\n'), /details\.start && recordingStream/);
});

test('the recording tees the pre-framing chunk, not each frame', () => {
    // frameAudioChunk returns subarrays of the same buffer, so writing frames
    // would produce an identical file with more syscalls — and if the split ever
    // changed, a subtly different one.
    const body = methodBody('private async *audioStream');
    const writeIdx = body.indexOf('recordingStream.write(chunk)');
    const frameIdx = body.indexOf('frameAudioChunk(chunk)');
    assert.ok(writeIdx > 0 && frameIdx > 0);
    assert.ok(writeIdx < frameIdx, 'the recording write should precede framing');
});

test('the audio recording and the transcript come from the same samples', () => {
    // The user-visible promise of a recording: it is what was transcribed. Both
    // now derive from the single ffmpeg on combined_audio.monitor in audioStream.
    const body = methodBody('private async *audioStream');
    assert.match(body, /yield \{ AudioEvent: \{ AudioChunk: frame \} \}/);
    assert.equal((src.match(/recordingStream\.write/g) || []).length, 1, 'exactly one writer');
});
