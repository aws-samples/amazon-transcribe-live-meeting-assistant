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

const renderPanel = ({ item = ITEM, segments = SEGMENTS, readOnly = false } = {}) => {
  onSave = vi.fn().mockResolvedValue(true);
  render(
    <SpeakerIdentitiesPanel
      item={item}
      segments={segments}
      setToolsOpen={() => {}}
      onSave={onSave}
      readOnly={readOnly}
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
