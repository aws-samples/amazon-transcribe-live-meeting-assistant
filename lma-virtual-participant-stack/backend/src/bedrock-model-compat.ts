// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * Model-shape differences between Bedrock Claude generations, for the VP's own
 * Bedrock calls.
 *
 * The AI DOM resolver talks to Bedrock through InvokeModel with the native
 * Anthropic messages body (`anthropic_version`, `max_tokens`, `temperature`)
 * rather than through Converse, so it cannot share the Python `bedrock_compat`
 * module the Lambdas use. The generation rule is the same one, repeated here for
 * the two settings that differ by model:
 *
 * - `temperature` is not accepted by Claude 5 and later. The resolver wants 0 and
 *   gets it on the models that take one.
 * - reasoning tokens are produced before any text and are billed against the
 *   output budget, so a budget sized for a non-reasoning model can be spent
 *   entirely on reasoning. The reasoning-capable generations get a larger one.
 *
 * The resolver's model is set by the `BEDROCK_DOM_RESOLVER_MODEL_ID` environment
 * variable on the task definition, which still defaults to Claude Haiku 4.5. This
 * exists so that overriding it to a newer generation works rather than failing
 * every selector resolution.
 */

/** Output budget for a non-reasoning model: the resolver answers with a short JSON object. */
export const DOM_RESOLVER_MAX_TOKENS = 600;

/**
 * Output budget for a reasoning-capable model. `max_tokens` is a ceiling rather
 * than a reservation, so a model that answers in 200 tokens is billed for 200
 * whichever figure it was offered.
 */
export const DOM_RESOLVER_REASONING_MAX_TOKENS = 4096;

/** First Claude major generation that rejects `temperature`. */
const TEMPERATURE_DROPPED_AT_MAJOR = 5;

/**
 * Claude ids carry the generation on either side of the family name depending on
 * vintage: `claude-3-5-sonnet-20240620-v1:0` puts it first, while
 * `claude-haiku-4-5-20251001-v1:0` and `claude-haiku-5-5` put the family first.
 * The optional letters cannot swallow the digits, so one pattern reads both.
 */
const CLAUDE_MAJOR = /claude-(?:[a-z]+-)?(\d+)/;

/** The major Claude generation named in `modelId`, or null if it is not a Claude model. */
export function claudeMajorVersion(modelId: string): number | null {
  if (!modelId || !modelId.includes('anthropic.')) return null;
  const match = CLAUDE_MAJOR.exec(modelId);
  return match ? Number(match[1]) : null;
}

/**
 * Whether `modelId` accepts a `temperature`.
 *
 * Read from the generation number rather than a list of exact ids, and an
 * unrecognised Claude generation at or above the cutoff is treated as refusing
 * one: sending a temperature to a model that refuses it fails every request,
 * while omitting it from a model that would have taken it only costs
 * determinism.
 */
export function acceptsTemperature(modelId: string): boolean {
  const major = claudeMajorVersion(modelId);
  if (major === null) return true;
  return major < TEMPERATURE_DROPPED_AT_MAJOR;
}

/** Whether `modelId` may spend output tokens on reasoning before it answers. */
export function emitsReasoning(modelId: string): boolean {
  const major = claudeMajorVersion(modelId);
  return major !== null && major >= TEMPERATURE_DROPPED_AT_MAJOR;
}

/**
 * The `max_tokens` / `temperature` half of a native Anthropic InvokeModel body,
 * sized and filtered for `modelId`.
 */
export function buildClaudeInvokeParams(
  modelId: string,
  temperature = 0,
): { max_tokens: number; temperature?: number } {
  const params: { max_tokens: number; temperature?: number } = {
    max_tokens: emitsReasoning(modelId)
      ? DOM_RESOLVER_REASONING_MAX_TOKENS
      : DOM_RESOLVER_MAX_TOKENS,
  };
  if (acceptsTemperature(modelId)) params.temperature = temperature;
  return params;
}
