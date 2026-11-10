/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Translating a meeting's transcript a line at a time, with Amazon Translate.
 *
 * Amazon Translate takes one text per TranslateText request, and a long
 * meeting has hundreds of lines. Sending them all at once -- which is what the
 * Translate toggle used to do, and then waited for every one of them before
 * showing any -- has the account's request rate refuse most of them, and the
 * page shows nothing for as long as the slowest request and its retries take.
 * This queue instead:
 *
 * - keeps a fixed number of requests in flight (TRANSLATE_CONCURRENCY);
 * - hands each translation back the moment it arrives, so lines fill in as they
 *   are done rather than all at the end;
 * - translates the lines the reader can see first (`prioritize`), and
 *   otherwise in the order they were queued;
 * - retries a request the service refused for rate or a transient fault, with
 *   exponential backoff and jitter, and reports one it cannot recover from;
 * - never queues the same line twice while it is waiting or in flight.
 *
 * It knows nothing of React or of the SDK: `translate` is the request, and the
 * callbacks are how the results leave. The hook in use-transcript-translation.js
 * is what wires it to the meeting page.
 */

/** Requests in flight at once. Small enough to stay inside Translate's default request rate. */
export const TRANSLATE_CONCURRENCY = 4;
/** Attempts per line, the first included, before it is reported as not translated. */
export const TRANSLATE_MAX_ATTEMPTS = 6;
export const TRANSLATE_BASE_DELAY_MS = 500;
export const TRANSLATE_MAX_DELAY_MS = 8000;

// Refusals that say "not now" rather than "not this": the request rate, or a
// fault on the service's side. Anything else -- a language pair Translate does
// not support, a text too long, no permission -- fails the same way every time.
const RETRYABLE_ERROR_NAMES = new Set([
  'ThrottlingException',
  'TooManyRequestsException',
  'ServiceUnavailableException',
  'InternalServerException',
  'RequestTimeout',
  'RequestTimeoutException',
  'TimeoutError',
  'NetworkError',
]);

/** Whether a failed TranslateText request is worth sending again. */
export const isRetryableTranslateError = (error) => {
  if (!error) return false;
  if (error.$retryable) return true;
  if (RETRYABLE_ERROR_NAMES.has(error.name) || RETRYABLE_ERROR_NAMES.has(error.code)) return true;
  const status = error.$metadata?.httpStatusCode;
  if (status === 429 || (status >= 500 && status < 600)) return true;
  // No response at all: the request never reached the service (a dropped
  // connection, a browser that went offline for a moment).
  return error instanceof TypeError && /fetch|network/i.test(error.message || '');
};

/** The wait before attempt `attempt + 1`: doubling from the base, capped, with full jitter. */
export const backoffDelay = (attempt, { baseDelayMs, maxDelayMs, random }) =>
  Math.floor(random() * Math.min(maxDelayMs, baseDelayMs * 2 ** attempt));

/** The key a line's translation is kept under: the segment and the language. */
export const translationKey = (segmentId, language) => `${segmentId}-${language}`;

/**
 * A short, readable reason for a line that could not be translated. Not the
 * SDK's message verbatim, which can run to a paragraph.
 */
export const translationErrorMessage = (error) => {
  const name = error?.name || error?.code || '';
  if (name === 'UnsupportedLanguagePairException') return 'Amazon Translate does not support this language pair';
  if (name === 'AccessDeniedException') return 'you do not have permission to use Amazon Translate';
  if (name === 'TextSizeLimitExceededException') return 'a line is too long to translate';
  if (isRetryableTranslateError(error)) return 'Amazon Translate is busy; try again in a moment';
  return error?.message || 'the translation request failed';
};

const defaultSleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

/**
 * A queue of lines to translate.
 *
 * `translate(job)` sends one request and resolves to the translated text. Jobs
 * are `{ key, text, ... }`; whatever else they carry is passed back. The
 * callbacks are not called after `stop()`.
 */
export const createTranslationQueue = ({
  translate,
  onTranslated = () => {},
  onFailed = () => {},
  onIdle = () => {},
  concurrency = TRANSLATE_CONCURRENCY,
  maxAttempts = TRANSLATE_MAX_ATTEMPTS,
  baseDelayMs = TRANSLATE_BASE_DELAY_MS,
  maxDelayMs = TRANSLATE_MAX_DELAY_MS,
  sleep = defaultSleep,
  random = Math.random,
}) => {
  // Insertion order is the order lines are translated in, after the visible ones.
  const pending = new Map();
  const inFlight = new Set();
  let priority = [];
  let stopped = false;

  const take = () => {
    const visible = priority.find((key) => pending.has(key));
    const key = visible ?? pending.keys().next().value;
    const job = pending.get(key);
    pending.delete(key);
    return job;
  };

  const attempt = async (job) => {
    for (let tries = 0; ; tries += 1) {
      try {
        // eslint-disable-next-line no-await-in-loop -- one line's attempts are sequential by nature
        const translated = await translate(job);
        if (!stopped) onTranslated(job, translated);
        return;
      } catch (error) {
        if (stopped) return;
        if (!isRetryableTranslateError(error) || tries + 1 >= maxAttempts) {
          onFailed(job, error);
          return;
        }
        // eslint-disable-next-line no-await-in-loop -- the backoff is the point
        await sleep(backoffDelay(tries, { baseDelayMs, maxDelayMs, random }));
        if (stopped) return;
      }
    }
  };

  let pump;
  const start = (job) => {
    inFlight.add(job.key);
    attempt(job).finally(() => {
      inFlight.delete(job.key);
      if (stopped) return;
      pump();
      if (!inFlight.size && !pending.size) onIdle();
    });
  };
  pump = () => {
    while (!stopped && inFlight.size < concurrency && pending.size) {
      start(take());
    }
  };

  return {
    /** Queue lines; one already waiting or in flight is not queued again. */
    enqueue(jobs) {
      if (stopped) return;
      jobs.forEach((job) => {
        if (!pending.has(job.key) && !inFlight.has(job.key)) pending.set(job.key, job);
      });
      pump();
    },
    /** The keys to translate before any other, in order: the lines on screen. */
    prioritize(keys) {
      priority = [...keys];
    },
    /** Drops every waiting line and silences the ones in flight. */
    stop() {
      stopped = true;
      pending.clear();
    },
    has(key) {
      return pending.has(key) || inFlight.has(key);
    },
    get waiting() {
      return pending.size;
    },
    get active() {
      return inFlight.size;
    },
  };
};
