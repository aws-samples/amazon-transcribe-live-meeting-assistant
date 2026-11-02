/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Recognising, recording and backing off from Nova Sonic's concurrent-stream
 * quota.
 *
 * Amazon Bedrock limits how many Nova Sonic bidirectional streams an account
 * may hold open at once in a region ("On-demand InvokeModel concurrent
 * requests for Amazon Nova 2 Sonic": 20, and not adjustable). The quota is
 * shared by every Virtual Participant in the account and region, and a
 * translator meeting holds two or three streams at once, so a busy deployment
 * reaches it.
 *
 * Measured against Amazon Bedrock (24 streams opened at once, us-east-1): 20
 * opened and 4 were refused, each by `send()` itself, about 290 ms after it was
 * called, with a `ThrottlingException`, HTTP status 429 and the message "Too
 * many connections, please wait before trying again." Nothing reached the
 * response stream. isNovaThrottle also accepts the same exception thrown from
 * the response stream, which the SDK does for an exception event, so that a
 * refusal delivered that way is not mistaken for an ordinary error.
 */

import { METRICS } from './metrics-emf.js';
import { NOVA_SONIC_STREAMS_THROTTLED } from './metrics-catalogue.js';
import { createLogger } from './logger.js';

const log = createLogger('nova-throttle');

/** What was opening the stream that was refused, for the log line. */
export type NovaThrottleContext =
  | 'assistant-session'
  | 'translator-turn'
  | 'tool-ack-clip';

/** Whether `error` is Bedrock refusing a stream for the concurrency quota. */
export function isNovaThrottle(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const e = error as { name?: unknown; message?: unknown; $metadata?: { httpStatusCode?: unknown } };
  if (e.name === 'ThrottlingException') return true;
  if (e.$metadata?.httpStatusCode === 429) return true;
  return typeof e.message === 'string' && /too many connections/i.test(e.message);
}

/**
 * How long to wait before attempt `attempt + 1`, given that `attempt` attempts
 * (counted from 0) have been refused.
 *
 * Exponential, capped, and jittered over the upper half of the interval, so
 * that several VPs refused at the same moment do not all come back at the same
 * moment and be refused together again.
 */
export function throttleBackoffMs(
  attempt: number,
  options: { baseMs?: number; capMs?: number; random?: () => number } = {},
): number {
  const baseMs = options.baseMs ?? 1000;
  const capMs = options.capMs ?? 30000;
  const random = options.random ?? Math.random;
  const ceiling = Math.min(capMs, baseMs * 2 ** Math.max(0, attempt));
  return Math.round(ceiling / 2 + random() * (ceiling / 2));
}

/**
 * Record one refused stream: a log line with a fixed `[nova-throttle]` marker,
 * and one `NovaSonicStreamsThrottled` datapoint.
 */
export function noteNovaThrottle(context: NovaThrottleContext, detail: string = ''): void {
  log.warn(
    `[nova-throttle] context=${context}${detail ? ` ${detail}` : ''}`
    + ' (Nova Sonic concurrent-stream quota reached for this account and region)',
  );
  METRICS.count(NOVA_SONIC_STREAMS_THROTTLED);
}
