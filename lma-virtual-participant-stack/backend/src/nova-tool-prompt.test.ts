/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting-assistant (strands_agent) section of the Nova Sonic system prompt.
 *
 * Nova Sonic is speech-to-speech, so whatever it "thinks" is spoken into the
 * meeting. These tests pin the shape of the prompt that asks for a one-sentence
 * preamble before the tool call and forbids spoken planning.
 *
 * What is NOT covered here: whether the model obeys the prompt. That needs a
 * live meeting.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { NovaAgent } from './nova-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';

const STRANDS_ARN = 'arn:aws:lambda:us-east-1:123456789012:function:strands';

function systemPrompt(meetingMode: MeetingMode, strandsLambdaArn?: string, activationMode = 'wake_phrase'): string {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode,
        meetingMode,
        strandsLambdaArn,
    });
    return (agent as any).buildSystemPromptContent();
}

test('the tool section forbids speaking reasoning or plans aloud', () => {
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const prompt = systemPrompt(mode, STRANDS_ARN);
        assert.match(prompt, /SPOKEN OUTPUT ONLY:/, `${mode} prompt must carry the spoken-output rule`);
        assert.match(prompt, /Never say your reasoning or your plan out loud/);
        assert.match(prompt, /never mention strands_agent or any other tool by name/);
    }
});

test('the tool section asks for a single preamble sentence before the call', () => {
    const prompt = systemPrompt('normal', STRANDS_ARN);
    assert.match(prompt, /say one short sentence to the person first/);
    assert.match(prompt, /Let me search for that information\. This may take a moment\./);
    assert.match(prompt, /Use strands_agent when users ask about:/);
});

test('the tool section is not a numbered procedure the model can recite', () => {
    // The step-by-step FIRST / WAIT / THEN workflow was recited back as a spoken
    // plan, and its wait-for-acknowledgement step has no meaning for a model
    // that calls tools asynchronously.
    const prompt = systemPrompt('normal', STRANDS_ARN);
    assert.doesNotMatch(prompt, /CRITICAL WORKFLOW/);
    assert.doesNotMatch(prompt, /WAIT for the user to acknowledge/);
    const toolSection = prompt.slice(prompt.indexOf('INFORMATION REQUESTS:'));
    assert.ok(toolSection.startsWith('INFORMATION REQUESTS:'));
    assert.doesNotMatch(toolSection, /^\s*\d\.\s/m);
});

test('the tool section is absent when no meeting-assistant tool is configured', () => {
    // The constructor falls back to STRANDS_LAMBDA_ARN, so clear it for this case.
    const saved = process.env.STRANDS_LAMBDA_ARN;
    delete process.env.STRANDS_LAMBDA_ARN;
    try {
        const prompt = systemPrompt('normal');
        assert.doesNotMatch(prompt, /INFORMATION REQUESTS:/);
        assert.doesNotMatch(prompt, /strands_agent/);
    } finally {
        if (saved !== undefined) process.env.STRANDS_LAMBDA_ARN = saved;
    }
});

test('the translator prompt carries no tool section', () => {
    const prompt = systemPrompt('translator', STRANDS_ARN, 'always_active');
    assert.doesNotMatch(prompt, /INFORMATION REQUESTS:/);
    assert.doesNotMatch(prompt, /SPOKEN OUTPUT ONLY:/);
});
