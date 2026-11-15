/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting page's transcript translations: what has been translated, how far
 * a translation has got, and why a line could not be translated.
 *
 * Every line on screen that has text and is final is translated once per
 * language and kept (by segment and language) for as long as the page is open,
 * so turning Translate off and on again, or going back to a language already
 * used, costs no requests. The requests go through the bounded, retrying queue
 * in components/call-panel/transcript-translation.js; see there for why.
 *
 * During a live meeting the newest lines are translated first, since that is
 * where the reader is; after it, the lines on screen first (`prioritize`, which
 * the transcript calls as it scrolls) and then from the top.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { TranslateTextCommand } from '@aws-sdk/client-translate';
import { ConsoleLogger } from 'aws-amplify/utils';

import {
  createTranslationQueue,
  translationErrorMessage,
  translationKey,
} from '../components/call-panel/transcript-translation';

const logger = new ConsoleLogger('useTranscriptTranslation');

/**
 * How often translations that have arrived are handed to the page. One state
 * update per arriving line would re-render the transcript hundreds of times
 * while a long meeting is translated.
 */
export const TRANSLATION_FLUSH_MS = 250;

/** Whether a segment is one this translates: a final line with text. */
const translatable = (segment) =>
  Boolean(segment?.segmentId) &&
  segment.isPartial !== true &&
  typeof segment.transcript === 'string' &&
  segment.transcript.trim() !== '';

const useTranscriptTranslation = ({
  segments,
  enabled,
  targetLanguage,
  translateClient,
  newestFirst = false,
  queueOptions = undefined,
}) => {
  // { [segmentId-language]: { cacheId, transcript, translated } }, the shape the
  // transcript rows read.
  const [cache, setCache] = useState({});
  // { [segmentId-language]: reason }
  const [failures, setFailures] = useState({});
  const [generation, setGeneration] = useState(0);
  const [queueReady, setQueueReady] = useState(0);

  // Read through refs so a new client (fresh credentials) or a new cache does
  // not tear down the queue and drop the requests it holds.
  const clientRef = useRef(translateClient);
  clientRef.current = translateClient;
  const cacheRef = useRef(cache);
  cacheRef.current = cache;
  const failuresRef = useRef(failures);
  failuresRef.current = failures;
  const queueRef = useRef(null);
  const priorityRef = useRef([]);

  const active = Boolean(enabled && targetLanguage);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    const arrived = {};
    const failed = {};
    let timer = null;
    const flush = () => {
      timer = null;
      if (Object.keys(arrived).length) {
        const batch = { ...arrived };
        Object.keys(arrived).forEach((key) => delete arrived[key]);
        setCache((state) => ({ ...state, ...batch }));
      }
      if (Object.keys(failed).length) {
        const batch = { ...failed };
        Object.keys(failed).forEach((key) => delete failed[key]);
        setFailures((state) => ({ ...state, ...batch }));
      }
    };
    const scheduleFlush = () => {
      if (timer === null) timer = setTimeout(flush, TRANSLATION_FLUSH_MS);
    };

    const queue = createTranslationQueue({
      ...queueOptions,
      translate: async (job) => {
        const data = await clientRef.current.send(
          new TranslateTextCommand({
            Text: job.text,
            SourceLanguageCode: 'auto',
            TargetLanguageCode: job.language,
          }),
        );
        return data?.TranslatedText ?? '';
      },
      onTranslated: (job, translated) => {
        arrived[job.key] = { cacheId: job.key, transcript: job.text, translated };
        scheduleFlush();
      },
      onFailed: (job, error) => {
        logger.debug('Could not translate a transcript line', job.key, error);
        failed[job.key] = translationErrorMessage(error);
        scheduleFlush();
      },
    });
    queue.prioritize(priorityRef.current.map((id) => translationKey(id, targetLanguage)));
    queueRef.current = queue;
    setQueueReady((n) => n + 1);
    return () => {
      queue.stop();
      if (timer !== null) clearTimeout(timer);
      // What had arrived is kept: it is a correct translation whatever happens next.
      flush();
      if (queueRef.current === queue) queueRef.current = null;
    };
  }, [active, targetLanguage, generation]);

  // The lines this language still needs, in the order they should be done.
  const wanted = useMemo(() => {
    if (!active) return [];
    const lines = segments.filter(translatable);
    if (newestFirst) lines.reverse();
    return lines.map((segment) => ({
      key: translationKey(segment.segmentId, targetLanguage),
      text: segment.transcript,
      language: targetLanguage,
    }));
  }, [segments, active, targetLanguage, newestFirst]);

  useEffect(() => {
    const queue = queueRef.current;
    if (!queue) return;
    queue.enqueue(
      wanted.filter(
        // Translated already, for this text (a segment rewritten since is done
        // again), or given up on until the reader asks again.
        (job) => cacheRef.current[job.key]?.transcript !== job.text && failuresRef.current[job.key] === undefined,
      ),
    );
  }, [wanted, queueReady]);

  /** The segment IDs on screen, top first: translated before the rest. */
  const prioritize = useCallback(
    (segmentIds) => {
      priorityRef.current = segmentIds;
      queueRef.current?.prioritize(segmentIds.map((id) => translationKey(id, targetLanguage)));
    },
    [targetLanguage],
  );

  /** Try the lines that failed again. */
  const retry = useCallback(() => {
    setFailures((state) =>
      Object.fromEntries(Object.entries(state).filter(([key]) => !key.endsWith(`-${targetLanguage}`))),
    );
    setGeneration((n) => n + 1);
  }, [targetLanguage]);

  const status = useMemo(() => {
    const total = wanted.length;
    let done = 0;
    let failed = 0;
    let reason = null;
    wanted.forEach((job) => {
      if (cache[job.key]?.transcript === job.text) done += 1;
      else if (failures[job.key] !== undefined) {
        failed += 1;
        reason = reason ?? failures[job.key];
      }
    });
    return { total, done, failed, reason, translating: active && done + failed < total };
  }, [wanted, cache, failures, active]);

  return { translateCache: cache, status, prioritize, retry };
};

export default useTranscriptTranslation;
