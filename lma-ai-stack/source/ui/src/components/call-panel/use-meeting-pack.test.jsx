/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting pack's currency key (use-meeting-pack.js): a part is built
 * again exactly when the key changes because one of its inputs did, a part
 * found empty is not built again until its inputs change, and a build a newer
 * one has replaced is stopped and its file never offered.
 */
import React, { useEffect } from 'react';
import PropTypes from 'prop-types';
import { afterEach, describe, it, expect, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';

import useMeetingPack from './use-meeting-pack';
import { PACK_PARTS, PartUnavailable } from './meeting-pack';

const IDS = PACK_PARTS.map((part) => part.id);
const BASE = {
  item: { callId: 'Weekly sync', callSummaryText: '{"SUMMARY":"ok"}' },
  participants: ['Speaker 1'],
  chapters: [],
  keyframeCount: 2,
  whiteboards: [{ WhiteboardId: 'w1', Version: 1 }],
};

const pdf = (size) => new Blob([new Uint8Array(size)]);
const builders = () => ({
  summary: vi.fn(async () => pdf(10)),
  slides: vi.fn(async () => pdf(20)),
  whiteboards: vi.fn(async () => pdf(30)),
});

// What the dialog does: build every part, again whenever the key changes.
const Dialog = ({ ctx, packBuilders, seen }) => {
  const pack = useMeetingPack(ctx, { builders: packBuilders });
  useEffect(() => {
    IDS.forEach((id) => pack.ensure(id));
  }, [pack.key, pack.ensure]);
  // eslint-disable-next-line no-param-reassign -- the test reads the latest pack
  seen.pack = pack;
  return null;
};
Dialog.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  ctx: PropTypes.object.isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  packBuilders: PropTypes.object.isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  seen: PropTypes.object.isRequired,
};

const calls = (spies) => Object.fromEntries(IDS.map((id) => [id, spies[id].mock.calls.length]));
const allReady = (seen) => IDS.every((id) => seen.pack?.parts[id]?.status === 'ready');

afterEach(() => cleanup());

describe('the currency key', () => {
  const CASES = [
    ['a whiteboard version', { whiteboards: [{ WhiteboardId: 'w1', Version: 2 }] }, ['whiteboards']],
    ['a whiteboard added', { whiteboards: [...BASE.whiteboards, { WhiteboardId: 'w2', Version: 1 }] }, ['whiteboards']],
    ["a speaker's name", { participants: ['Ana Lima'] }, ['summary']],
    ['the summary', { item: { ...BASE.item, callSummaryText: '{"SUMMARY":"new"}' } }, ['summary']],
    ['a chapter', { chapters: [{ title: 'Budget', start: 30 }] }, ['slides']],
    ['the number of key frames', { keyframeCount: 3 }, ['slides']],
    ['nothing (the same inputs, new objects)', { whiteboards: [{ WhiteboardId: 'w1', Version: 1 }] }, []],
  ];

  it.each(CASES)('changes with %s, and only that part is built again', async (_name, change, rebuilt) => {
    const spies = builders();
    const seen = {};
    const { rerender } = render(<Dialog ctx={BASE} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(allReady(seen)).toBe(true));
    const before = seen.pack.key;
    expect(calls(spies)).toEqual({ summary: 1, slides: 1, whiteboards: 1 });

    rerender(<Dialog ctx={{ ...BASE, ...change }} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(allReady(seen)).toBe(true));
    expect(seen.pack.key !== before).toBe(rebuilt.length > 0);
    expect(calls(spies)).toEqual(Object.fromEntries(IDS.map((id) => [id, rebuilt.includes(id) ? 2 : 1])));
  });

  it('builds a part found empty again only once its inputs change', async () => {
    const spies = builders();
    spies.slides.mockRejectedValueOnce(new PartUnavailable('All camera.'));
    const seen = {};
    const { rerender } = render(<Dialog ctx={BASE} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(seen.pack?.parts.slides?.status).toBe('unavailable'));
    rerender(<Dialog ctx={{ ...BASE }} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(seen.pack.parts.whiteboards?.status).toBe('ready'));
    expect(spies.slides).toHaveBeenCalledTimes(1);
    expect(seen.pack.parts.slides).toMatchObject({ status: 'unavailable', message: 'All camera.' });

    rerender(<Dialog ctx={{ ...BASE, keyframeCount: 3 }} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(seen.pack.parts.slides?.status).toBe('ready'));
    expect(spies.slides).toHaveBeenCalledTimes(2);
  });

  it('stops a build a newer one replaced, and offers only the newer file', async () => {
    const spies = builders();
    let finishFirst;
    let firstCancelled;
    spies.slides.mockImplementationOnce(
      (_ctx, { isCancelled }) =>
        new Promise((resolve) => {
          firstCancelled = isCancelled;
          finishFirst = () => resolve(pdf(999));
        }),
    );
    const seen = {};
    const { rerender } = render(<Dialog ctx={BASE} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(firstCancelled).toBeTypeOf('function'));
    expect(firstCancelled()).toBe(false);

    rerender(<Dialog ctx={{ ...BASE, keyframeCount: 3 }} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(seen.pack.parts.slides?.status).toBe('ready'));
    expect(firstCancelled()).toBe(true);
    finishFirst();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(seen.pack.parts.slides.size).toBe(20);
  });

  it('offers a file built from inputs that have since changed as being built, not ready', async () => {
    const spies = builders();
    spies.whiteboards.mockImplementationOnce(async () => pdf(30)).mockImplementationOnce(() => new Promise(() => {}));
    const seen = {};
    const { rerender } = render(<Dialog ctx={BASE} packBuilders={spies} seen={seen} />);
    await waitFor(() => expect(allReady(seen)).toBe(true));
    rerender(
      <Dialog ctx={{ ...BASE, whiteboards: [{ WhiteboardId: 'w1', Version: 2 }] }} packBuilders={spies} seen={seen} />,
    );
    expect(seen.pack.parts.whiteboards.status).toBe('building');
    expect(seen.pack.parts.summary.status).toBe('ready');
  });
});
