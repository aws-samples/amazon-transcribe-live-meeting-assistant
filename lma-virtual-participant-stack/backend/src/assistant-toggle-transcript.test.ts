/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Switching the assistant's mode mid-meeting must not interrupt the transcript.
 *
 * A pause in what the assistant can hear is the accepted cost of rebuilding its
 * Nova session. A gap in the transcript is not: the transcript is the product,
 * and a few seconds missing from the middle of a meeting is not recoverable.
 *
 * The two are independent because the audio graph keeps them apart, and this
 * asserts that at the source level — like `scribe-asr-channels.test.ts`, whose
 * method-body approach it reuses. Three facts hold it up:
 *
 *   1. With a voice assistant, Amazon Transcribe hears `meeting_audio.monitor`
 *      and `agent_output.monitor` as the two channels of one stream, through
 *      capture processes owned by TranscriptionService (scribe.ts), which the
 *      assistant's session lifecycle does not touch. Without one, and for the
 *      recordings, it reads `combined_audio.monitor`; the Virtual Participant's
 *      own pacat stream is the sole writer of meeting audio into that sink, and
 *      it is also what keeps the null sink from suspending.
 *   2. The assistant's voice reaches `combined_audio` (the recordings' sink)
 *      through a PulseAudio `module-loopback` loaded by entrypoint.sh — a module
 *      in the sound server, not a child process of the Node app, so nothing the
 *      agent does to its own session can take it down.
 *   3. The reconfiguration path is Bedrock protocol events and agent state and
 *      nothing else. It starts no process and stops none, including the one
 *      audio process the agent does own: the pacat stream it plays its own voice
 *      into.
 *
 * `assistant-reconfigure.test.ts` asserts (3) behaviourally, by spying on the
 * audio stream for every mode transition. This file asserts it structurally, so
 * that a future rebuild which reaches for a process has to change a test that
 * says why it must not.
 *
 * What is NOT covered here: whether PulseAudio actually keeps the sink running
 * across a rebuild, and whether Amazon Transcribe sees an unbroken stream. Both
 * need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

/** Read a source file from src/, whether the test runs from src/ or dist/. */
function source(name: string): string {
    return readFileSync(new URL(`./${name}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');
}

const novaAgent = source('nova-agent.ts');
const scribe = source('scribe.ts');
// src/ and dist/ are siblings under backend/, so this resolves either way.
const entrypoint = readFileSync(new URL('../entrypoint.sh', import.meta.url).pathname, 'utf8');

/**
 * The body of a method, by brace matching from its signature. More exact than a
 * regex on the next member's indentation, which matters here because the claim
 * being asserted is about what a specific method does and does not contain.
 */
function methodBody(src: string, signature: string): string {
    const start = src.indexOf(signature);
    assert.notEqual(start, -1, `signature not found: ${signature}`);
    const open = src.indexOf('{', start + signature.length - 1);
    assert.notEqual(open, -1, `no body for: ${signature}`);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth += 1;
        else if (src[i] === '}') {
            depth -= 1;
            if (depth === 0) return src.slice(open, i + 1);
        }
    }
    assert.fail(`unbalanced body for: ${signature}`);
}

const RECONFIGURE_PATH = [
    'async reconfigure(request: AssistantReconfigureRequest)',
    'private applyMeetingMode(mode: MeetingMode)',
    'private applyActivationMode(mode: ActivationMode)',
    'private async rebuildSessionForNewMode()',
    'private async refreshIntoNewMode()',
    'private scheduleSessionRecovery()',
    'private async waitWhileRefreshing()',
];

test('nothing on the reconfiguration path starts or stops an audio process', () => {
    for (const signature of RECONFIGURE_PATH) {
        const body = methodBody(novaAgent, signature);
        assert.doesNotMatch(body, /\bspawn\(/, `${signature} must not spawn a process`);
        assert.doesNotMatch(body, /pacat/, `${signature} must not touch the playback stream`);
        assert.doesNotMatch(body, /ffmpeg/, `${signature} must not touch an audio capture`);
        assert.doesNotMatch(body, /combined_audio|meeting_audio|agent_output/, `${signature} must not touch a sink`);
    }
});

test('the reconfiguration path reaches the model session and the agent own state, and nothing else', () => {
    // Positively: the rebuild goes through the same three session lifecycle
    // methods the keep-alive refresh and the translator transitions already use.
    const rebuild = methodBody(novaAgent, 'private async rebuildSessionForNewMode()');
    assert.match(rebuild, /this\.closeSession\(\)/);
    assert.match(rebuild, /this\.createSession\(\)/);
    assert.match(rebuild, /this\.refreshIntoNewMode\(\)/);
    assert.match(methodBody(novaAgent, 'private async refreshIntoNewMode()'), /this\.refreshSession\(\)/);

    // And `refreshSession` itself, which predates this feature, is Bedrock
    // protocol events plus a session swap.
    const refresh = methodBody(novaAgent, 'private async refreshSession()');
    assert.doesNotMatch(refresh, /\bspawn\(/);
    assert.doesNotMatch(refresh, /pacat/);
    assert.match(refresh, /sessionEnd/);
    assert.match(refresh, /this\.createSession\(true\)/);
});

test('the one audio process the agent owns writes to its own sink, not the transcribed one', () => {
    // So even if a rebuild did take the playback stream down, the sink
    // transcription reads is not the one it writes to.
    const playback = methodBody(novaAgent, 'private ensurePacatStream()');
    assert.match(playback, /--device=agent_output/);
    assert.doesNotMatch(playback, /combined_audio/);

    // And that is the only process the agent starts at all.
    const spawnSites = novaAgent.split('\n').filter((line) => /\bspawn\(/.test(line));
    assert.equal(spawnSites.length, 1, `expected one spawn site in nova-agent.ts, found: ${spawnSites.join(' | ')}`);
});

test('meeting audio reaches the combined sink through the Virtual Participant own stream', () => {
    // The sole writer, and what keeps the null sink from suspending (#542,
    // #569). It belongs to TranscriptionService, which the assistant's session
    // lifecycle does not touch — as do the two captures the two-channel
    // stream is built from.
    assert.match(methodBody(scribe, 'private startAgentAudioCapture('), /agent_output\.monitor/);
    const fanout = methodBody(scribe, 'private startMeetingAudioFanout(');
    assert.match(fanout, /spawn\('pacat'/);
    assert.match(fanout, /--device=combined_audio/);
    // It is gated on transcription being live and on the pause flag — not on
    // anything about the assistant's mode or session.
    assert.match(fanout, /this\.isTranscribing/);
    assert.match(fanout, /details\.start/);
    assert.doesNotMatch(fanout, /meetingMode/);
    assert.doesNotMatch(fanout, /reconfigure/);
    assert.doesNotMatch(fanout, /isActive\(\)\s*\|\|/, 'the fanout must not stop when the assistant session goes');
});

test('the assistant voice reaches the combined sink through the sound server, not the app', () => {
    // A module-loopback inside PulseAudio. Rebuilding the Nova session cannot
    // unload it, so the sink stays alive and keeps carrying meeting audio
    // whatever happens to the assistant. The agent_output sink the
    // assistant's channel is captured from is a null sink loaded the same way,
    // so it too outlives any session.
    assert.match(entrypoint, /module-null-sink sink_name=combined_audio/);
    assert.match(entrypoint, /module-loopback source=agent_output\.monitor sink=combined_audio/);
    assert.match(entrypoint, /module-null-sink sink_name=agent_output/);
});

test('the mode the transcription path reads has one writer, so a toggle cannot leave it stale', () => {
    // On the Amazon Transcribe path the mode is read for every result on the
    // assistant's channel, which is not written in translator mode (the
    // translation is written from the assistant's own text instead). Both the
    // configuration loader and a runtime toggle publish through the same
    // function, which is also the one place to change when the configuration
    // record moves into a unified store.
    const assistantChannel = methodBody(scribe, 'private handleAssistantChannelResult(');
    assert.match(assistantChannel, /writesAssistantChannel\(details\.meetingMode\)/);

    const writers = source('assistant-toggles.ts').match(/details\.meetingMode =/g) ?? [];
    assert.equal(writers.length, 1, 'the shared mode must have exactly one writer');
    for (const file of ['nova-agent.ts', 'nova-sonic-config-loader.ts', 'elevenlabs-agent.ts']) {
        assert.doesNotMatch(source(file), /details\.meetingMode =/, `${file} must publish through the one writer`);
    }
    assert.match(source('nova-agent.ts'), /publishMeetingMode\(mode\)/);
    assert.match(source('nova-sonic-config-loader.ts'), /publishMeetingMode\(/);
});

test('switching into translator mode mid-meeting leaves the assistant transcript channel as it started', () => {
    // A known limitation, pinned rather than left to be discovered. On the
    // MicroVM engine path the assistant's own transcript channel is opened, or
    // not, once when the engine run starts, from the meeting mode at that
    // moment — translator mode does not open one, because its output is a
    // translation of speech already in the transcript. A mode change after that
    // point does not revisit the decision, so a meeting switched into translator
    // mode keeps transcribing the assistant for the rest of the run.
    //
    // This is a difference in what appears in the transcript, not a gap in it:
    // the meeting channel is untouched either way. Closing it would need the
    // engine run to react to a mode change rather than read it once.
    const run = methodBody(scribe, 'private async runMicrovmTranscriptionOnce(');
    assert.match(run, /voiceAssistant\.isEnabled\(\) && details\.meetingMode !== 'translator'/);
    // The decision is made once, before the audio loop, from a local const.
    assert.match(run, /const agentSession =/);
    assert.equal(run.match(/details\.meetingMode/g)?.length, 1, 'the mode is consulted once per engine run');
});
