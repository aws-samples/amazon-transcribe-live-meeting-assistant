/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The transcript route from "Hey Alex, stop talking" to a silent assistant.
 *
 * This is the supplementary path. A repeated wake phrase on its own has nothing
 * to tell an assistant that is already listening, so it used to be dropped;
 * recognising the stop intent here silences the assistant without depending on
 * the model noticing. The primary path remains the `mute` tool, because a
 * transcript segment is only finalized after the speaker pauses.
 *
 * The provider is selected from the environment at import time, so the imports
 * below are deliberately dynamic.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

process.env.VOICE_ASSISTANT_PROVIDER = 'amazon_nova_sonic';
process.env.VOICE_ASSISTANT_ACTIVATION_MODE = 'wake_phrase';

const { transcriptionService } = await import('./scribe.js');
const { voiceAssistant } = await import('./voice-assistant.js');

// Stand in for the Bedrock bidirectional stream. The segments below can reach
// the ordinary activation path, which would otherwise open a real session.
(voiceAssistant as any).createSession = async () => {
    (voiceAssistant as any)._isActive = true;
};

/** Hand the scribe a completed Amazon Transcribe result, as the live stream would. */
function finalSegment(text: string): void {
    (transcriptionService as any).processTranscriptResult({
        Alternatives: [{ Transcript: text, Items: [] }],
        IsPartial: false,
    });
}

/** The assistant mid-answer: activated, with a live session, and audible. */
function assistantAnswering(): void {
    (voiceAssistant as any)._isActivated = true;
    (voiceAssistant as any)._isActive = true;
    (voiceAssistant as any)._isMuted = false;
}

test('a stop request in a completed segment silences the answering assistant', () => {
    assistantAnswering();
    finalSegment('Hey Alex, stop talking');
    assert.equal(voiceAssistant.isMuted(), true);
});

test('a follow-up question leaves the assistant talking', () => {
    // The wake phrase is there and the assistant is already activated, so this
    // segment reaches exactly the same code path as the stop request above.
    assistantAnswering();
    finalSegment('Hey Alex, can you stop the recording and summarise the last hour?');
    assert.equal(voiceAssistant.isMuted(), false);
});

test('a stop request is ignored while the assistant is not listening', async () => {
    // The stop path is scoped to an assistant that is actually listening, so an
    // idle one has nothing to silence and the segment takes the ordinary
    // wake-phrase activation path instead. The assistant does wake up, and the
    // STOP REQUESTS prompt block is what then answers "Okay" and mutes it; the
    // mute state asserted here is the state immediately after the segment, not
    // the end of that exchange.
    (voiceAssistant as any)._isActivated = false;
    (voiceAssistant as any)._isActive = false;
    (voiceAssistant as any)._isMuted = false;
    finalSegment('Hey Alex, stop talking');
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(voiceAssistant.isMuted(), false);
    voiceAssistant.deactivate();
});

test('a partial transcript never triggers a stop', async () => {
    // Partials are revised as the speaker continues: "Hey Alex, stop" is a
    // prefix of "Hey Alex, stop the recording and send me the notes".
    assistantAnswering();
    (transcriptionService as any).processTranscriptResult({
        Alternatives: [{ Transcript: 'Hey Alex, stop', Items: [] }],
        IsPartial: true,
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(voiceAssistant.isMuted(), false);
    voiceAssistant.deactivate();
});
