/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * What translating a long transcript has to get right.
 *
 * A 58-minute meeting has a few hundred lines, and Amazon Translate takes one
 * text per request. The queue has to translate every one of them without ever
 * having more than a handful of requests open, ride out the service refusing
 * some for rate, hand each translation back as it arrives rather than at the
 * end, do the lines on screen first, and say which lines it gave up on.
 */
import { describe, it, expect, vi } from 'vitest';

import {
  TRANSLATE_CONCURRENCY,
  TRANSLATE_MAX_ATTEMPTS,
  backoffDelay,
  createTranslationQueue,
  isRetryableTranslateError,
  translationErrorMessage,
} from './transcript-translation';

// About what a 58-minute meeting produces.
const LONG_MEETING_LINES = 415;

const jobsFor = (count) =>
  Array.from({ length: count }, (_, i) => ({ key: `seg-${i}-nl`, text: `line ${i}`, language: 'nl' }));

const throttled = () => Object.assign(new Error('Rate exceeded'), { name: 'TooManyRequestsException' });

/** A fake TranslateText: answers after a tick, counting how many are open at once. */
const fakeTranslate = ({ fail = () => null } = {}) => {
  const state = { open: 0, maxOpen: 0, calls: [] };
  state.translate = async (job) => {
    state.calls.push(job.key);
    state.open += 1;
    state.maxOpen = Math.max(state.maxOpen, state.open);
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    state.open -= 1;
    const error = fail(job, state.calls.filter((k) => k === job.key).length);
    if (error) throw error;
    return `vertaald ${job.text}`;
  };
  return state;
};

/** Runs the queue to completion; resolves with what it reported. */
const drain = (options, jobs, before = () => {}) =>
  new Promise((resolve) => {
    const translated = new Map();
    const failed = new Map();
    const order = [];
    const queue = createTranslationQueue({
      sleep: () => Promise.resolve(),
      ...options,
      onTranslated: (job, text) => {
        translated.set(job.key, text);
        order.push(job.key);
      },
      onFailed: (job, error) => failed.set(job.key, error),
      onIdle: () => resolve({ translated, failed, order, queue }),
    });
    before(queue);
    queue.enqueue(jobs);
  });

describe('translating a long transcript', () => {
  it('translates every line of a long meeting', async () => {
    const api = fakeTranslate();
    const { translated, failed } = await drain({ translate: api.translate }, jobsFor(LONG_MEETING_LINES));
    expect(translated.size).toBe(LONG_MEETING_LINES);
    expect(failed.size).toBe(0);
    expect(translated.get('seg-414-nl')).toBe('vertaald line 414');
  });

  it('never has more than a few requests open at once', async () => {
    const api = fakeTranslate();
    await drain({ translate: api.translate }, jobsFor(LONG_MEETING_LINES));
    expect(api.maxOpen).toBeLessThanOrEqual(TRANSLATE_CONCURRENCY);
    expect(api.maxOpen).toBeGreaterThan(1);
  });

  it('hands each translation back as it arrives rather than all at the end', async () => {
    const api = fakeTranslate();
    let firstArrivedWhileWaiting = null;
    const queue = createTranslationQueue({
      translate: api.translate,
      onTranslated: () => {
        if (firstArrivedWhileWaiting === null) firstArrivedWhileWaiting = queue.waiting;
      },
    });
    const idle = new Promise((resolve) => {
      const check = setInterval(() => {
        if (!queue.waiting && !queue.active) {
          clearInterval(check);
          resolve();
        }
      }, 1);
    });
    queue.enqueue(jobsFor(LONG_MEETING_LINES));
    await idle;
    expect(firstArrivedWhileWaiting).toBeGreaterThan(LONG_MEETING_LINES / 2);
  });

  it('retries a line the service refused for rate, and translates it', async () => {
    // Every line is refused twice before it goes through: the shape of a burst
    // the account's request rate cannot take.
    const api = fakeTranslate({ fail: (_job, attempt) => (attempt <= 2 ? throttled() : null) });
    const sleep = vi.fn(() => Promise.resolve());
    const { translated, failed } = await drain({ translate: api.translate, sleep }, jobsFor(50));
    expect(translated.size).toBe(50);
    expect(failed.size).toBe(0);
    expect(api.calls.length).toBe(150);
    expect(sleep).toHaveBeenCalledTimes(100);
  });

  it('waits longer after each refusal, within a cap', () => {
    const options = { baseDelayMs: 500, maxDelayMs: 8000, random: () => 0.999 };
    const waits = [0, 1, 2, 3, 4, 5, 6].map((attempt) => backoffDelay(attempt, options));
    expect(waits.slice(0, 5)).toEqual([499, 999, 1998, 3996, 7992]);
    expect(Math.max(...waits)).toBeLessThan(8000);
  });

  it('gives up on a line still refused after the last attempt, and reports it', async () => {
    const api = fakeTranslate({ fail: (job) => (job.key === 'seg-3-nl' ? throttled() : null) });
    const { translated, failed } = await drain({ translate: api.translate }, jobsFor(10));
    expect(translated.size).toBe(9);
    expect([...failed.keys()]).toEqual(['seg-3-nl']);
    expect(api.calls.filter((k) => k === 'seg-3-nl')).toHaveLength(TRANSLATE_MAX_ATTEMPTS);
  });

  it('does not retry a refusal that would be refused again, and reports it', async () => {
    const unsupported = Object.assign(new Error('nope'), { name: 'UnsupportedLanguagePairException' });
    const api = fakeTranslate({ fail: () => unsupported });
    const { failed } = await drain({ translate: api.translate }, jobsFor(5));
    expect(failed.size).toBe(5);
    expect(api.calls).toHaveLength(5);
    expect(translationErrorMessage(unsupported)).toMatch(/does not support this language pair/);
  });

  it('translates the lines on screen first', async () => {
    const api = fakeTranslate();
    const onScreen = ['seg-300-nl', 'seg-301-nl', 'seg-302-nl', 'seg-303-nl'];
    const { order } = await drain({ translate: api.translate, concurrency: 1 }, jobsFor(LONG_MEETING_LINES), (q) =>
      q.prioritize(onScreen),
    );
    expect(order.slice(0, 4)).toEqual(onScreen);
    expect(order).toHaveLength(LONG_MEETING_LINES);
  });

  it('does not send a line twice while it is waiting or in flight', async () => {
    const api = fakeTranslate();
    const jobs = jobsFor(20);
    const { translated } = await drain({ translate: api.translate }, jobs, (q) => {
      q.enqueue(jobs);
      q.enqueue(jobs.slice(0, 5));
    });
    expect(translated.size).toBe(20);
    expect(api.calls).toHaveLength(20);
  });

  it('reports nothing once stopped', async () => {
    const api = fakeTranslate();
    const onTranslated = vi.fn();
    const queue = createTranslationQueue({ translate: api.translate, onTranslated });
    queue.enqueue(jobsFor(20));
    queue.stop();
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(onTranslated).not.toHaveBeenCalled();
    expect(api.calls.length).toBeLessThanOrEqual(TRANSLATE_CONCURRENCY);
  });
});

describe('isRetryableTranslateError', () => {
  it('retries rate refusals and service faults', () => {
    expect(isRetryableTranslateError(throttled())).toBe(true);
    expect(isRetryableTranslateError({ name: 'ThrottlingException' })).toBe(true);
    expect(isRetryableTranslateError({ name: 'X', $metadata: { httpStatusCode: 503 } })).toBe(true);
    expect(isRetryableTranslateError({ name: 'X', $metadata: { httpStatusCode: 429 } })).toBe(true);
    expect(isRetryableTranslateError(new TypeError('Failed to fetch'))).toBe(true);
  });

  it('does not retry what would fail the same way again', () => {
    expect(isRetryableTranslateError({ name: 'AccessDeniedException', $metadata: { httpStatusCode: 400 } })).toBe(
      false,
    );
    expect(isRetryableTranslateError({ name: 'UnsupportedLanguagePairException' })).toBe(false);
    expect(isRetryableTranslateError(null)).toBe(false);
  });
});
