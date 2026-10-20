// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * The AI DOM resolver's Bedrock body is assembled from the model id, because two
 * of its settings are not accepted by every model the resolver can be pointed at.
 * The model comes from the BEDROCK_DOM_RESOLVER_MODEL_ID environment variable on
 * the task definition, so an operator can move it to a generation the hard-coded
 * body would have failed against.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DOM_RESOLVER_MAX_TOKENS,
  DOM_RESOLVER_REASONING_MAX_TOKENS,
  acceptsTemperature,
  buildClaudeInvokeParams,
  claudeMajorVersion,
} from './bedrock-model-compat.js';

test('the generation is read from either Claude naming scheme', () => {
  // Claude 3.x names the generation before the family, Claude 4+ after it.
  assert.equal(claudeMajorVersion('anthropic.claude-3-haiku-20240307-v1:0'), 3);
  assert.equal(claudeMajorVersion('us.anthropic.claude-3-7-sonnet-20250219-v1:0'), 3);
  assert.equal(claudeMajorVersion('global.anthropic.claude-haiku-4-5-20251001-v1:0'), 4);
  assert.equal(claudeMajorVersion('us.anthropic.claude-sonnet-4-6'), 4);
  assert.equal(claudeMajorVersion('global.anthropic.claude-haiku-5-5'), 5);
  assert.equal(claudeMajorVersion('us.anthropic.claude-opus-5-5'), 5);
  // Not a Claude model.
  assert.equal(claudeMajorVersion('amazon.nova-2-sonic-v1:0'), null);
  assert.equal(claudeMajorVersion(''), null);
});

test('the resolver keeps temperature 0 on the models that accept one', () => {
  // The resolver reads a DOM and returns a selector; the same page should resolve
  // to the same selector twice.
  assert.equal(acceptsTemperature('global.anthropic.claude-haiku-4-5-20251001-v1:0'), true);
  const params = buildClaudeInvokeParams('global.anthropic.claude-haiku-4-5-20251001-v1:0');
  assert.deepEqual(params, { max_tokens: DOM_RESOLVER_MAX_TOKENS, temperature: 0 });
});

test('no temperature is sent to a model that rejects one', () => {
  // Claude 5 and later refuse a request that carries one, so pointing the
  // resolver at one would otherwise fail every selector resolution rather than
  // falling back to the hard-coded selectors.
  assert.equal(acceptsTemperature('global.anthropic.claude-haiku-5-5'), false);
  const params = buildClaudeInvokeParams('global.anthropic.claude-haiku-5-5');
  assert.equal(params.temperature, undefined);
  assert.ok(!('temperature' in params));
});

test('a reasoning-capable model gets a budget that leaves room for reasoning', () => {
  // Reasoning tokens are produced before any text and are billed against the
  // same budget, so the 600 that suffices for a non-reasoning model can be spent
  // entirely on reasoning and return no selector at all.
  const params = buildClaudeInvokeParams('us.anthropic.claude-sonnet-5-5');
  assert.equal(params.max_tokens, DOM_RESOLVER_REASONING_MAX_TOKENS);
  assert.ok(params.max_tokens > 2048);
});

test('a non-reasoning model keeps the smaller budget', () => {
  // max_tokens is a ceiling rather than a charge, but there is no reason to widen
  // it for a model that answers in a couple of hundred tokens.
  assert.equal(
    buildClaudeInvokeParams('global.anthropic.claude-haiku-4-5-20251001-v1:0').max_tokens,
    DOM_RESOLVER_MAX_TOKENS,
  );
  assert.equal(buildClaudeInvokeParams('amazon.nova-pro-v1:0').max_tokens, DOM_RESOLVER_MAX_TOKENS);
});

test('an unrecognised later Claude generation is assumed to refuse a temperature', () => {
  // Sending one to a model that refuses it fails every request; omitting it from
  // one that would have taken it only costs determinism.
  assert.equal(acceptsTemperature('global.anthropic.claude-haiku-9-1'), false);
});
