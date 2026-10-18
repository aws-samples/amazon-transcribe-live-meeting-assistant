/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Speakers panel must get right.
 *
 * The panel writes the whole identity map on every edit, and that map is what
 * four separate readers resolve a speaker through -- the live transcript, the
 * download, the embeddable transcript and (in Python) every LLM-facing
 * transcript. So the contract worth pinning is the SHAPE of what it writes: a
 * name stored under the key built from the channel and the stored speaker, a
 * merge stored as a redirect between two such keys, and the pasted attendee list
 * stored as plain candidates. A panel that renders correctly but writes the wrong
 * key silently names nobody.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import SpeakerIdentitiesPanel from './SpeakerIdentitiesPanel';

/* eslint-disable react/prop-types */

const SEGMENTS = [
  {
    channel: 'CALLER',
    speaker: 'Other Participant (spk_0)',
    startTime: 0,
    endTime: 4,
    isPartial: false,
    segmentId: 's1',
  },
  {
    channel: 'CALLER',
    speaker: 'Other Participant (spk_0)',
    startTime: 9,
    endTime: 11,
    isPartial: false,
    segmentId: 's2',
  },
  {
    channel: 'CALLER',
    speaker: 'Other Participant (spk_1)',
    startTime: 5,
    endTime: 7,
    isPartial: false,
    segmentId: 's3',
  },
];

const ITEM = { callId: 'c1', callerPhoneNumber: 'Conference Room 3', speakerIdentities: undefined };

let onSave;

let onIdentify;

const renderPanel = ({
  item = ITEM,
  segments = SEGMENTS,
  readOnly = false,
  identifyResult = { Success: true, NamesApplied: 2 },
  isLive = false,
} = {}) => {
  onSave = vi.fn().mockResolvedValue(true);
  onIdentify = vi.fn().mockResolvedValue(identifyResult);
  render(
    <SpeakerIdentitiesPanel
      item={item}
      segments={segments}
      setToolsOpen={() => {}}
      onSave={onSave}
      onIdentify={onIdentify}
      readOnly={readOnly}
      isLive={isLive}
    />,
  );
};

/** The map as it was written, parsed back out of the serialized argument. */
const savedMap = (call = 0) => JSON.parse(onSave.mock.calls[call][0]);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SpeakerIdentitiesPanel', () => {
  it('lists one row per voice, not one per segment', () => {
    renderPanel();
    // Three segments, two voices.
    expect(screen.getByText(/2 turns/)).toBeTruthy();
    expect(screen.getByText(/1 turn ·/)).toBeTruthy();
  });

  it('shows how many segments a rename will move', () => {
    // The instinct is to assume an edit touches the line you are looking at, so
    // the turn count is the panel telling you otherwise.
    renderPanel();
    expect(screen.getByText(/2 turns · 0:06/)).toBeTruthy();
  });

  it('falls back to the participant name from the Stream Audio form as the placeholder', () => {
    renderPanel();
    expect(screen.getAllByPlaceholderText('Conference Room 3').length).toBe(2);
  });

  it('stores a typed name under the key built from the channel and stored speaker', async () => {
    renderPanel();
    const [first] = screen.getAllByPlaceholderText('Conference Room 3');
    fireEvent.change(first, { target: { value: 'Bob Strahan' } });
    fireEvent.blur(first);

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const map = savedMap();
    expect(map.identities['CALLER#Other Participant (spk_0)']).toMatchObject({
      name: 'Bob Strahan',
      status: 'confirmed',
      source: 'manual',
    });
  });

  it('records a manual name as confirmed, so it is not shown as a guess', async () => {
    renderPanel();
    const [first] = screen.getAllByPlaceholderText('Conference Room 3');
    fireEvent.change(first, { target: { value: 'Bob' } });
    fireEvent.blur(first);
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(savedMap().identities['CALLER#Other Participant (spk_0)'].status).toBe('confirmed');
  });

  it('does not write when the field is blurred unchanged', () => {
    // Every write is a full-map replacement, so a no-op blur would race a
    // concurrent edit for nothing.
    renderPanel();
    const [first] = screen.getAllByPlaceholderText('Conference Room 3');
    fireEvent.blur(first);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('clearing a name removes the mapping rather than storing an empty one', async () => {
    renderPanel({
      item: {
        ...ITEM,
        speakerIdentities: JSON.stringify({
          identities: { 'CALLER#Other Participant (spk_0)': { name: 'Bob' } },
        }),
      },
    });
    const field = screen.getByDisplayValue('Bob');
    fireEvent.change(field, { target: { value: '' } });
    fireEvent.blur(field);

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(savedMap().identities['CALLER#Other Participant (spk_0)']).toBeUndefined();
  });

  it('keeps an existing name in the field so it can be edited', () => {
    renderPanel({
      item: {
        ...ITEM,
        speakerIdentities: JSON.stringify({
          identities: { 'CALLER#Other Participant (spk_0)': { name: 'Bob' } },
        }),
      },
    });
    expect(screen.getByDisplayValue('Bob')).toBeTruthy();
  });

  it('folds merged voices into a single row', () => {
    renderPanel({
      item: {
        ...ITEM,
        speakerIdentities: JSON.stringify({
          merges: { 'CALLER#Other Participant (spk_1)': 'CALLER#Other Participant (spk_0)' },
        }),
      },
    });
    // All three segments now belong to one person.
    expect(screen.getByText(/3 turns/)).toBeTruthy();
    expect(screen.getByText('+1 merged')).toBeTruthy();
  });

  it('turns a pasted attendee list into candidates', async () => {
    renderPanel();
    fireEvent.click(screen.getByText('Known attendees (optional)'));
    const textarea = screen.getByPlaceholderText(/Bob Strahan <bob@example.com>/);
    fireEvent.change(textarea, {
      target: { value: 'Bob Strahan <bob@example.com>\nSarah Lee (Organizer)' },
    });
    fireEvent.click(screen.getByText('Add attendees'));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(savedMap().candidates).toEqual(['Bob Strahan', 'Sarah Lee']);
  });

  it('works with no attendee list at all, which is the normal case', async () => {
    // A meeting with no roster must behave identically -- plain text entry.
    renderPanel();
    const [first] = screen.getAllByPlaceholderText('Conference Room 3');
    fireEvent.change(first, { target: { value: 'Someone Unlisted' } });
    fireEvent.blur(first);
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(savedMap().identities['CALLER#Other Participant (spk_0)'].name).toBe('Someone Unlisted');
    expect(savedMap().candidates).toEqual([]);
  });

  it('writes a complete map every time, so a reader never sees a half-shape', async () => {
    renderPanel();
    const [first] = screen.getAllByPlaceholderText('Conference Room 3');
    fireEvent.change(first, { target: { value: 'Bob' } });
    fireEvent.blur(first);
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(Object.keys(savedMap()).sort()).toEqual([
      'candidates',
      'identities',
      'merges',
      'rejected',
      'segmentOverrides',
      'version',
    ]);
  });

  it('offers nothing to edit on a meeting the user may only read', () => {
    renderPanel({ readOnly: true });
    screen.getAllByPlaceholderText('Conference Room 3').forEach((field) => {
      expect(field.disabled).toBe(true);
    });
  });

  it('says so plainly when no voices have been heard yet', () => {
    renderPanel({ segments: [] });
    expect(screen.getByText(/No speakers yet/)).toBeTruthy();
  });
});

describe('identifying speakers from the transcript', () => {
  it('runs a forced pass when the button is pressed', async () => {
    // Forced: a person asked, so answer them even if the backend's own shortcut
    // would have skipped the model.
    renderPanel();
    fireEvent.click(screen.getByText('Identify speakers').closest('button'));
    await waitFor(() => expect(onIdentify).toHaveBeenCalledWith({ force: true }));
  });

  it('says how many speakers were identified', async () => {
    renderPanel({ identifyResult: { Success: true, NamesApplied: 2 } });
    fireEvent.click(screen.getByText('Identify speakers').closest('button'));
    await waitFor(() => expect(screen.getByText('Identified 2 speakers')).toBeTruthy());
  });

  it('says plainly when the transcript does not name anybody yet', async () => {
    // The alternative is silence, which reads as a broken button.
    renderPanel({ identifyResult: { Success: true, NamesApplied: 0 } });
    fireEvent.click(screen.getByText('Identify speakers').closest('button'));
    await waitFor(() => expect(screen.getByText(/Nothing in the transcript names them yet/)).toBeTruthy());
  });

  it('reports a failed pass', async () => {
    renderPanel({ identifyResult: { Success: false, NamesApplied: 0 } });
    fireEvent.click(screen.getByText('Identify speakers').closest('button'));
    await waitFor(() => expect(screen.getByText('Could not identify speakers')).toBeTruthy());
  });

  it('offers no button to a user who may only read the meeting', () => {
    renderPanel({ readOnly: true });
    expect(screen.getByText('Identify speakers').closest('button').disabled).toBe(true);
  });

  it('runs an unforced pass on a timer while the meeting is live', async () => {
    // The behaviour with no test before: the interval was keyed on a callback
    // recreated every render, so it was torn down and rebuilt on every arriving
    // transcript segment and only ever elapsed during silence.
    vi.useFakeTimers();
    try {
      renderPanel({ isLive: true, identifyResult: { Success: true, NamesApplied: 0 } });
      expect(onIdentify).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(46000);
      expect(onIdentify).toHaveBeenCalledWith({ force: false });
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the timer running even when the callback changes identity', async () => {
    // The actual bug: the interval depended on a prop that React recreates on
    // every render, and a live meeting re-renders on every arriving segment, so
    // the timer was reset faster than it could elapse.
    vi.useFakeTimers();
    try {
      const calls = [];
      const Harness = ({ tick }) => (
        <SpeakerIdentitiesPanel
          item={ITEM}
          segments={SEGMENTS}
          setToolsOpen={() => {}}
          onSave={vi.fn()}
          // A new function identity on every render, as a plain inline callback
          // would be.
          onIdentify={() => {
            calls.push(tick);
            return Promise.resolve({ Success: true, NamesApplied: 0 });
          }}
          readOnly={false}
          isLive
        />
      );
      const { rerender } = render(<Harness tick={0} />);
      // Re-render repeatedly, as arriving transcript segments would.
      // Sequential on purpose: each re-render must follow the tick before it.
      /* eslint-disable no-await-in-loop */
      for (let i = 1; i <= 8; i += 1) {
        await vi.advanceTimersByTimeAsync(6000);
        rerender(<Harness tick={i} />);
      }
      /* eslint-enable no-await-in-loop */
      expect(calls.length).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops the automatic passes once every voice is confirmed', () => {
    vi.useFakeTimers();
    try {
      renderPanel({
        isLive: true,
        item: {
          ...ITEM,
          speakerIdentities: JSON.stringify({
            identities: {
              'CALLER#Other Participant (spk_0)': { name: 'Bob', status: 'confirmed' },
              'CALLER#Other Participant (spk_1)': { name: 'Sarah', status: 'confirmed' },
            },
          }),
        },
      });
      vi.advanceTimersByTime(120000);
      expect(onIdentify).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps running while a voice holds only an unconfirmed suggestion', async () => {
    // The backend treats a suggestion as still open, so a later
    // self-introduction can upgrade it. The UI must agree or it stops early.
    vi.useFakeTimers();
    try {
      renderPanel({
        isLive: true,
        identifyResult: { Success: true, NamesApplied: 0 },
        item: {
          ...ITEM,
          speakerIdentities: JSON.stringify({
            identities: {
              'CALLER#Other Participant (spk_0)': { name: 'Bob', status: 'proposed' },
              'CALLER#Other Participant (spk_1)': { name: 'Sarah', status: 'proposed' },
            },
          }),
        },
      });
      await vi.advanceTimersByTimeAsync(46000);
      expect(onIdentify).toHaveBeenCalledWith({ force: false });
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not start an automatic pass on an ended meeting', () => {
    vi.useFakeTimers();
    try {
      renderPanel({ isLive: false });
      vi.advanceTimersByTime(120000);
      expect(onIdentify).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('a suggested name', () => {
  const proposed = {
    ...ITEM,
    speakerIdentities: JSON.stringify({
      identities: {
        'CALLER#Other Participant (spk_0)': {
          name: 'Bob Strahan',
          status: 'proposed',
          source: 'addressed-by-name',
        },
      },
    }),
  };

  it('is marked as suggested rather than shown as settled', () => {
    renderPanel({ item: proposed });
    expect(screen.getByText('suggested')).toBeTruthy();
  });

  it("becomes the user's own name when confirmed", async () => {
    // Recorded as manual, which is what stops a later pass revisiting it.
    renderPanel({ item: proposed });
    fireEvent.click(screen.getByText('Confirm'));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const entry = savedMap().identities['CALLER#Other Participant (spk_0)'];
    expect(entry.status).toBe('confirmed');
    expect(entry.source).toBe('manual');
    expect(entry.name).toBe('Bob Strahan');
  });

  it('is removed and remembered when rejected', async () => {
    // Remembered so an automatic pass cannot propose the same name straight back.
    renderPanel({ item: proposed });
    fireEvent.click(screen.getByText('Not Bob Strahan'));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const map = savedMap();
    expect(map.identities['CALLER#Other Participant (spk_0)']).toBeUndefined();
    expect(map.rejected['CALLER#Other Participant (spk_0)']).toEqual(['Bob Strahan']);
  });
});
