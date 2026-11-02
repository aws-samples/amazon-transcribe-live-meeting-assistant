/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Renders the short "looking that up" acknowledgement the Nova Sonic agent
 * plays itself when the model calls `strands_agent` without saying anything
 * first (see NovaAgent's `[tool-ack]` handling).
 *
 * The clip is spoken by Nova Sonic, in the voice the agent is configured with,
 * through a short session of its own that is opened once and closed as soon as
 * the audio has arrived. Rendering it ahead of time is what lets the fallback be
 * immediate and predictable when it is needed: no model round trip, no chance
 * of the model treating a nudge as a new request and calling the tool again.
 */

import {
  BedrockRuntimeClient,
  InvokeModelWithBidirectionalStreamCommand,
  InvokeModelWithBidirectionalStreamInput,
} from '@aws-sdk/client-bedrock-runtime';
import { randomUUID } from 'crypto';
import { isNovaThrottle, noteNovaThrottle, throttleBackoffMs } from './nova-throttle.js';
import { createLogger } from './logger.js';

const log = createLogger('tool-ack-clip');

/** What the fallback says. One short sentence that is true whatever was asked. */
export const TOOL_ACK_PHRASE = 'One moment, let me look that up.';

/** The longest clip accepted. The phrase takes under two seconds to say; a
 *  render that runs on is the model saying something else, and is discarded
 *  rather than played into a meeting. */
export const TOOL_ACK_MAX_CLIP_MS = 4000;

/** How long a render may take before it is abandoned. */
const RENDER_TIMEOUT_MS = 15000;

/** Playback PCM format, which is also what Nova returns: 16 kHz, 16-bit, mono. */
const SAMPLE_RATE = 16000;
const BYTES_PER_MS = (SAMPLE_RATE * 2) / 1000;

/** Silence sent on the session's audio input while the render runs: Nova
 *  expects a live audio stream alongside text input. 100 ms per chunk. */
const SILENCE_CHUNK = Buffer.alloc(BYTES_PER_MS * 100).toString('base64');

/** Duration of a 16 kHz 16-bit mono PCM buffer, in milliseconds. */
export function pcmDurationMs(pcm: Buffer): number {
  return Math.round(pcm.length / BYTES_PER_MS);
}

/** How many times a render refused for Nova Sonic's concurrency quota is tried
 *  in all, and the backoff between tries. Generous, because nothing waits on
 *  the clip: it runs in the background and is needed only once somebody asks
 *  the assistant something. Four tries span roughly 20 to 35 seconds. */
export const TOOL_ACK_RENDER_ATTEMPTS = 4;
export const TOOL_ACK_RENDER_BACKOFF = { baseMs: 5000, capMs: 30000 };

export interface RenderOptions {
  /** Tries in all when the render is refused for the concurrency quota. */
  attempts?: number;
  /** Checked before each retry; a render for an agent that has stopped is
   *  abandoned rather than opening another stream. */
  shouldContinue?: () => boolean;
  /** Stand-ins for tests. */
  sleep?: (ms: number) => Promise<void>;
  random?: () => number;
}

/**
 * Ask Nova Sonic to speak `phrase` in `voiceId` and return the audio as PCM.
 *
 * Resolves to null on any failure, including a clip longer than
 * TOOL_ACK_MAX_CLIP_MS — the caller then has no fallback clip, which costs the
 * acknowledgement and nothing else. A render refused because the account's
 * Nova Sonic streams are all in use is tried again later, with backoff, up to
 * TOOL_ACK_RENDER_ATTEMPTS times, and then skipped.
 */
export async function renderToolAckClip(
  client: BedrockRuntimeClient,
  modelId: string,
  voiceId: string,
  phrase: string = TOOL_ACK_PHRASE,
  options: RenderOptions = {},
): Promise<Buffer | null> {
  const attempts = Math.max(1, options.attempts ?? TOOL_ACK_RENDER_ATTEMPTS);
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  for (let attempt = 0; attempt < attempts; attempt++) {
    const result = await renderOnce(client, modelId, voiceId, phrase);
    if (!result.throttled) return result.clip;
    noteNovaThrottle('tool-ack-clip', `attempt=${attempt + 1}/${attempts}`);
    if (attempt + 1 >= attempts) break;
    const delay = throttleBackoffMs(attempt, { ...TOOL_ACK_RENDER_BACKOFF, random: options.random });
    log.warn(`[tool-ack] action=render-throttled attempt=${attempt + 1} retry_in_ms=${delay}`);
    await sleep(delay);
    if (options.shouldContinue && !options.shouldContinue()) {
      log.warn('[tool-ack] action=render-abandoned reason=agent-stopped');
      return null;
    }
  }
  log.warn(`[tool-ack] action=render-skipped reason=throttled attempts=${attempts}`);
  return null;
}

async function renderOnce(
  client: BedrockRuntimeClient,
  modelId: string,
  voiceId: string,
  phrase: string,
): Promise<{ clip: Buffer | null; throttled: boolean }> {
  const promptName = randomUUID();
  const systemId = randomUUID();
  const textId = randomUUID();
  const audioId = randomUUID();
  let done = false;

  const encoder = new TextEncoder();
  const chunk = (event: unknown): InvokeModelWithBidirectionalStreamInput => ({
    chunk: { bytes: encoder.encode(JSON.stringify({ event })) },
  });

  async function* body(): AsyncIterable<InvokeModelWithBidirectionalStreamInput> {
    yield chunk({
      sessionStart: { inferenceConfiguration: { maxTokens: 256, topP: 0.9, temperature: 0.1 } },
    });
    yield chunk({
      promptStart: {
        promptName,
        textOutputConfiguration: { mediaType: 'text/plain' },
        audioOutputConfiguration: {
          audioType: 'SPEECH',
          encoding: 'base64',
          mediaType: 'audio/lpcm',
          sampleRateHertz: SAMPLE_RATE,
          sampleSizeBits: 16,
          channelCount: 1,
          voiceId,
        },
      },
    });
    yield chunk({
      contentStart: {
        promptName, contentName: systemId, type: 'TEXT', interactive: false, role: 'SYSTEM',
        textInputConfiguration: { mediaType: 'text/plain' },
      },
    });
    yield chunk({
      textInput: {
        promptName,
        contentName: systemId,
        content: 'You are a voice that reads text aloud. When the user sends a message, say that '
          + 'message aloud exactly as written, word for word, and say nothing else.',
      },
    });
    yield chunk({ contentEnd: { promptName, contentName: systemId } });
    yield chunk({
      contentStart: {
        promptName, contentName: audioId, type: 'AUDIO', interactive: true, role: 'USER',
        audioInputConfiguration: {
          audioType: 'SPEECH', encoding: 'base64', mediaType: 'audio/lpcm',
          sampleRateHertz: SAMPLE_RATE, sampleSizeBits: 16, channelCount: 1,
        },
      },
    });
    yield chunk({ audioInput: { promptName, contentName: audioId, content: SILENCE_CHUNK } });
    yield chunk({
      contentStart: {
        promptName, contentName: textId, type: 'TEXT', interactive: true, role: 'USER',
        textInputConfiguration: { mediaType: 'text/plain' },
      },
    });
    yield chunk({ textInput: { promptName, contentName: textId, content: phrase } });
    yield chunk({ contentEnd: { promptName, contentName: textId } });
    const deadline = Date.now() + RENDER_TIMEOUT_MS;
    while (!done && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      yield chunk({ audioInput: { promptName, contentName: audioId, content: SILENCE_CHUNK } });
    }
    yield chunk({ contentEnd: { promptName, contentName: audioId } });
    yield chunk({ promptEnd: { promptName } });
    yield chunk({ sessionEnd: {} });
  }

  const startedAt = Date.now();
  const parts: Buffer[] = [];
  let spoken = '';
  let inAssistantAudio = false;
  try {
    const response = await client.send(
      new InvokeModelWithBidirectionalStreamCommand({ modelId, body: body() }),
    );
    const decoder = new TextDecoder();
    for await (const event of response.body ?? []) {
      if (Date.now() - startedAt > RENDER_TIMEOUT_MS) break;
      const bytes = (event as any).chunk?.bytes;
      if (!bytes) continue;
      let parsed: any;
      try {
        parsed = JSON.parse(decoder.decode(bytes));
      } catch {
        continue;
      }
      const ev = parsed.event ?? {};
      if (ev.contentStart?.type === 'AUDIO') {
        inAssistantAudio = true;
      } else if (ev.textOutput && ev.textOutput.role === 'ASSISTANT' && !spoken) {
        spoken = String(ev.textOutput.content ?? '');
      } else if (ev.audioOutput && inAssistantAudio) {
        parts.push(Buffer.from(ev.audioOutput.content, 'base64'));
      } else if (ev.contentEnd?.type === 'AUDIO' && parts.length > 0) {
        // One audio block is the whole phrase; anything after it is not wanted.
        break;
      }
    }
  } catch (error) {
    if (isNovaThrottle(error)) return { clip: null, throttled: true };
    log.warn('[tool-ack] action=render-failed', error instanceof Error ? error.message : String(error));
    return { clip: null, throttled: false };
  } finally {
    done = true;
  }

  const clip = Buffer.concat(parts);
  const clipMs = pcmDurationMs(clip);
  if (clip.length === 0 || clipMs > TOOL_ACK_MAX_CLIP_MS) {
    log.warn(
      `[tool-ack] action=render-discarded audio_ms=${clipMs} render_ms=${Date.now() - startedAt}`
      + ` voice=${voiceId}`,
    );
    return { clip: null, throttled: false };
  }
  log.info(
    `[tool-ack] action=rendered audio_ms=${clipMs} render_ms=${Date.now() - startedAt} voice=${voiceId}`
    + (spoken ? ` text="${spoken.trim()}"` : ''),
  );
  return { clip, throttled: false };
}
