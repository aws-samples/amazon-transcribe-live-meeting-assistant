/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The Translate toggle on a long meeting.
 *
 * Driven through the hook the transcript uses, with a fake Amazon Translate
 * client in place of the SDK's, over a fake transcript the size of a 58-minute
 * meeting. What is pinned: every line is translated in the end, never with more
 * than a few requests open, through the service refusing some for rate; a line
 * that cannot be translated is said so on screen, with a way to try again,
 * rather than the transcript silently staying untranslated.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';

import useTranscriptTranslation from './use-transcript-translation';
import { TranslationProgress } from '../components/call-panel/CallPanel';
import { TRANSLATE_CONCURRENCY, translationKey } from '../components/call-panel/transcript-translation';

const LONG_MEETING_LINES = 415;
const LANGUAGE = 'nl';

const transcript = (count) =>
  Array.from({ length: count }, (_, i) => ({
    segmentId: `seg-${i}`,
    transcript: `line ${i}`,
    isPartial: false,
    channel: i % 2 ? 'REMOTE' : 'LOCAL',
  }));

const throttled = () => Object.assign(new Error('Rate exceeded'), { name: 'TooManyRequestsException' });

/** Stands in for TranslateClient: `send` answers after a tick, or with what `fail` returns. */
const fakeClient = ({ fail = () => null, delayMs = 0 } = {}) => {
  const client = { open: 0, maxOpen: 0, sent: [] };
  client.send = vi.fn(async (command) => {
    const { Text, TargetLanguageCode } = command.input;
    client.sent.push(Text);
    client.open += 1;
    client.maxOpen = Math.max(client.maxOpen, client.open);
    await new Promise((resolve) => {
      setTimeout(resolve, delayMs);
    });
    client.open -= 1;
    const error = fail(Text, client.sent.filter((t) => t === Text).length);
    if (error) throw error;
    return { TranslatedText: `[${TargetLanguageCode}] ${Text}` };
  });
  return client;
};

const INSTANT_RETRIES = { sleep: () => Promise.resolve() };

const translate = (segments, client, extra = {}) =>
  renderHook(
    (props) =>
      useTranscriptTranslation({
        segments,
        enabled: true,
        targetLanguage: LANGUAGE,
        translateClient: client,
        queueOptions: INSTANT_RETRIES,
        ...props,
      }),
    { initialProps: extra },
  );

const LONG_WAIT = { timeout: 10000 };

describe('translating a long meeting', () => {
  it('translates every line in the end', async () => {
    const segments = transcript(LONG_MEETING_LINES);
    const { result } = translate(segments, fakeClient());
    await waitFor(() => expect(result.current.status.done).toBe(LONG_MEETING_LINES), LONG_WAIT);
    expect(result.current.status.translating).toBe(false);
    expect(result.current.translateCache[translationKey('seg-414', LANGUAGE)].translated).toBe('[nl] line 414');
  });

  it('never has more than a few requests open at once', async () => {
    const client = fakeClient();
    const { result } = translate(transcript(LONG_MEETING_LINES), client);
    await waitFor(() => expect(result.current.status.done).toBe(LONG_MEETING_LINES), LONG_WAIT);
    expect(client.maxOpen).toBeLessThanOrEqual(TRANSLATE_CONCURRENCY);
    expect(client.send).toHaveBeenCalledTimes(LONG_MEETING_LINES);
  });

  it('shows lines as they arrive, before the whole meeting is done', async () => {
    // Slow enough that the whole meeting takes several flushes of the cache.
    const { result } = translate(transcript(LONG_MEETING_LINES), fakeClient({ delayMs: 5 }));
    await waitFor(() => expect(result.current.status.done).toBeGreaterThan(0), LONG_WAIT);
    expect(result.current.status.done).toBeLessThan(LONG_MEETING_LINES);
    expect(result.current.status.translating).toBe(true);
  });

  it('rides out the service refusing requests for rate', async () => {
    // Every line is refused once before it goes through.
    const client = fakeClient({ fail: (_text, attempt) => (attempt === 1 ? throttled() : null) });
    const { result } = translate(transcript(LONG_MEETING_LINES), client);
    await waitFor(() => expect(result.current.status.done).toBe(LONG_MEETING_LINES), LONG_WAIT);
    expect(result.current.status.failed).toBe(0);
    expect(client.send).toHaveBeenCalledTimes(LONG_MEETING_LINES * 2);
  });

  it('says which lines could not be translated, and tries them again when asked', async () => {
    let refuse = true;
    const denied = Object.assign(new Error('denied'), { name: 'AccessDeniedException' });
    const client = fakeClient({ fail: (text) => (refuse && text.endsWith('7') ? denied : null) });
    const { result } = translate(transcript(100), client);
    await waitFor(() => expect(result.current.status.translating).toBe(false), LONG_WAIT);
    expect(result.current.status.failed).toBe(10);
    expect(result.current.status.reason).toMatch(/permission/);

    const onRetry = vi.fn();
    render(<TranslationProgress status={result.current.status} live={false} onRetry={onRetry} />);
    expect(screen.getByText(/could not translate 10 of 100 lines/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);

    refuse = false;
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status.done).toBe(100), LONG_WAIT);
    expect(result.current.status.failed).toBe(0);
  });

  it('shows its progress while it translates', () => {
    render(
      <TranslationProgress
        status={{ total: 415, done: 40, failed: 0, reason: null, translating: true }}
        live={false}
        onRetry={() => {}}
      />,
    );
    expect(screen.getByText('Translating 40 of 415 lines')).toBeInTheDocument();
  });

  it('does not translate a line twice, nor partial or empty lines', async () => {
    const segments = [
      ...transcript(20),
      { segmentId: 'partial', transcript: 'still being said', isPartial: true },
      { segmentId: 'blank', transcript: '   ', isPartial: false },
    ];
    const client = fakeClient();
    const { result, rerender } = translate(segments, client);
    await waitFor(() => expect(result.current.status.done).toBe(20), LONG_WAIT);
    // A re-render with the same lines (a new segment arriving elsewhere, a
    // rename) sends nothing more.
    rerender({ segments: [...segments] });
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(client.send).toHaveBeenCalledTimes(20);
  });

  it('keeps what was translated when the toggle goes off and on again', async () => {
    const client = fakeClient();
    const segments = transcript(30);
    const { result, rerender } = translate(segments, client);
    await waitFor(() => expect(result.current.status.done).toBe(30), LONG_WAIT);
    rerender({ enabled: false });
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.status.done).toBe(30), LONG_WAIT);
    expect(client.send).toHaveBeenCalledTimes(30);
  });

  it('translates the newest lines first during a live meeting', async () => {
    const client = fakeClient();
    const { result } = translate(transcript(40), client, { newestFirst: true });
    await waitFor(() => expect(result.current.status.done).toBe(40), LONG_WAIT);
    expect(client.sent[0]).toBe('line 39');
  });
});
