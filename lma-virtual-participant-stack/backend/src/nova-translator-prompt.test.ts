/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The translator-mode session that NovaAgent sends to Nova Sonic: its system
 * prompt's language rules and the voice in its promptStart.
 *
 * What is NOT covered here: whether the model speaks the language the prompt
 * asks for. That was measured against Nova 2 Sonic itself (the figures are in
 * the comment above the rules in NovaAgent.buildSystemPromptContent) and needs
 * a live meeting to confirm.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { NovaAgent, translatorDirectionRules } from './nova-agent.js';

function translatorAgent(a: string, b: string, voiceId = 'tiffany'): NovaAgent {
    return new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'always_active',
        meetingMode: 'translator',
        translatorLanguageA: a,
        translatorLanguageB: b,
        voiceId,
    });
}

/** Run the agent's session setup against a stand-in session and return what it queued. */
async function queuedSetupEvents(agent: NovaAgent, isRefresh: boolean): Promise<any[]> {
    const inner = agent as any;
    inner.session = {
        queue: [],
        isActive: true,
        promptName: 'prompt',
        audioContentId: 'audio',
        isPromptStartSent: false,
        isAudioContentStartSent: false,
        sessionStartTime: Date.now(),
        lastRefreshTime: Date.now(),
    };
    await inner.setupInitialEvents(isRefresh);
    return inner.session.queue.map((entry: any) => entry.event);
}

test('the translator prompt names the target language for each direction', () => {
    const prompt = (translatorAgent('English', 'Spanish') as any).buildSystemPromptContent() as string;
    assert.match(prompt, /When you hear English, say it in Spanish\. When you hear Spanish, say it in English\./);
    assert.match(prompt, /replying in English to English, or in Spanish to Spanish, is always wrong/);
    assert.match(prompt, /Your speech and your text must be the same words in the same language/);
    // The earlier wording left the choice of language to the model.
    assert.doesNotMatch(prompt, /naturally in English or Spanish/);
    assert.doesNotMatch(prompt, /detect whether it is in/);
});

test('the direction rules follow the configured language pair', () => {
    const rules = translatorDirectionRules('French', 'German');
    assert.equal(rules[0], '1. When you hear French, say it in German. When you hear German, say it in French.');
    assert.match(rules[1], /replying in French to French, or in German to German, is always wrong/);
    const prompt = (translatorAgent('French', 'German') as any).buildSystemPromptContent() as string;
    for (const rule of rules) {
        assert.ok(prompt.includes(rule), `prompt must carry: ${rule}`);
    }
    assert.doesNotMatch(prompt, /English|Spanish/);
});

test('every translator session carries the configured voice and the translator prompt', async () => {
    for (const isRefresh of [false, true]) {
        const agent = translatorAgent('English', 'Spanish', 'matthew');
        // A refresh in another mode replays the conversation so far, so give it a turn to replay.
        (agent as any).conversationHistory = [
            { role: 'USER', content: 'good morning everyone', timestamp: Date.now() },
        ];
        const events = await queuedSetupEvents(agent, isRefresh);

        const promptStart = events.find((e) => e.promptStart)?.promptStart;
        assert.ok(promptStart, 'a promptStart must be queued');
        assert.equal(promptStart.audioOutputConfiguration.voiceId, 'matthew');
        assert.deepEqual(
            promptStart.toolConfiguration.tools.map((t: any) => t.toolSpec.name),
            ['mute'],
        );

        const systemBlock = events.find((e) => e.contentStart?.role === 'SYSTEM')?.contentStart;
        assert.ok(systemBlock, 'a SYSTEM content block must be queued');
        const systemText = events.find(
            (e) => e.textInput?.contentName === systemBlock.contentName,
        )?.textInput.content as string;
        assert.match(systemText, /^TRANSLATOR MODE:/);
        assert.match(systemText, /When you hear English, say it in Spanish\./);

        // Not even on a refresh: earlier turns are what a translator session
        // drifts on (translator-turns.ts).
        const replayed = events.filter((e) => e.textInput?.content === 'good morning everyone');
        assert.equal(replayed.length, 0, 'a translator session never replays history');
    }
});
