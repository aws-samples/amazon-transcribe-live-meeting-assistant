/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The voice assistant factory hands every provider the activation duration the
 * deployment configured (VOICE_ASSISTANT_ACTIVATION_DURATION), so the window
 * the assistant stays awake after a wake phrase is the same whichever provider
 * is selected.
 *
 * What is NOT covered here: the timer actually expiring during a live meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, test } from 'node:test';
import { createVoiceAssistant, createVoiceAssistantFromEnv } from './voice-assistant.js';
import { NovaAgent } from './nova-agent.js';
import { ElevenLabsAgent } from './elevenlabs-agent.js';

const ENV_KEYS = ['VOICE_ASSISTANT_PROVIDER', 'VOICE_ASSISTANT_ACTIVATION_DURATION', 'ELEVENLABS_API_KEY'];
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

function activationDuration(agent: unknown): number {
  return (agent as any).defaultActivationDuration;
}

test('createVoiceAssistant passes the configured activation duration to Nova Sonic', () => {
  const agent = createVoiceAssistant({
    provider: 'amazon_nova_sonic',
    activationMode: 'wake_phrase',
    activationDuration: 90,
    modelId: 'amazon.nova-2-sonic-v1:0',
    systemPrompt: 'You are Alex, an AI meeting assistant.',
  });
  assert.ok(agent instanceof NovaAgent);
  assert.equal(activationDuration(agent), 90);
});

test('createVoiceAssistant passes the configured activation duration to ElevenLabs', () => {
  const agent = createVoiceAssistant({
    provider: 'elevenlabs',
    activationMode: 'wake_phrase',
    activationDuration: 45,
    apiKey: 'test-key',
  });
  assert.ok(agent instanceof ElevenLabsAgent);
  assert.equal(activationDuration(agent), 45);
});

test('Nova Sonic keeps the 30 second default when no duration is configured', () => {
  const agent = createVoiceAssistant({
    provider: 'amazon_nova_sonic',
    activationMode: 'wake_phrase',
    modelId: 'amazon.nova-2-sonic-v1:0',
    systemPrompt: 'You are Alex, an AI meeting assistant.',
  });
  assert.ok(agent instanceof NovaAgent);
  assert.equal(activationDuration(agent), 30);
});

test('VOICE_ASSISTANT_ACTIVATION_DURATION reaches a Nova Sonic agent built from the environment', () => {
  process.env.VOICE_ASSISTANT_PROVIDER = 'amazon_nova_sonic';
  process.env.VOICE_ASSISTANT_ACTIVATION_DURATION = '120';
  const agent = createVoiceAssistantFromEnv();
  assert.ok(agent instanceof NovaAgent);
  assert.equal(activationDuration(agent), 120);
});

test('VOICE_ASSISTANT_ACTIVATION_DURATION reaches an ElevenLabs agent built from the environment', () => {
  process.env.VOICE_ASSISTANT_PROVIDER = 'elevenlabs';
  process.env.VOICE_ASSISTANT_ACTIVATION_DURATION = '75';
  process.env.ELEVENLABS_API_KEY = 'test-key';
  const agent = createVoiceAssistantFromEnv();
  assert.ok(agent instanceof ElevenLabsAgent);
  assert.equal(activationDuration(agent), 75);
});
