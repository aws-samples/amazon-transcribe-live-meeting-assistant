/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The speaker modal opened from a speaker's chip on the Transcript tab.
 *
 * It writes the same identity map the Speakers panel on the Details tab does,
 * so what is pinned here is what it writes: a name under the voice's key, a
 * merge as a redirect from one voice key to another, voices combined into the
 * first, and merged voices separated again -- all in one write, on Save.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import SpeakerAssignmentModal, { applySpeakerAssignment } from './SpeakerAssignmentModal';
import { buildSpeakerRoster, emptyIdentities, parseSpeakerIdentities } from '../common/speaker-identities';

const A = 'REMOTE#Other Participant (spk_0)';
const B = 'REMOTE#Other Participant (spk_1)';
const C = 'REMOTE#Other Participant (spk_2)';

const seg = (speaker, startTime, channel = 'REMOTE') => ({
  channel,
  speaker,
  startTime,
  endTime: startTime + 2,
  isPartial: false,
  segmentId: `${speaker}-${startTime}`,
});
const SEGMENTS = [
  seg('Other Participant (spk_0)', 0),
  seg('Other Participant (spk_1)', 5),
  seg('Other Participant (spk_0)', 9),
  seg('Other Participant (spk_2)', 12),
  seg('Me', 15, 'LOCAL'),
];

const setup = ({
  identities = emptyIdentities(),
  speakerKeys = [A],
  speaker = 'Room 3 (spk_0)',
  readOnly = false,
  saved = true,
} = {}) => {
  const roster = buildSpeakerRoster(SEGMENTS, identities, 'Room 3');
  const rows = roster.filter((row) => speakerKeys.includes(row.key));
  const onSave = vi.fn().mockResolvedValue(saved);
  const onDismiss = vi.fn();
  render(
    <SpeakerAssignmentModal
      speaker={speaker}
      rows={rows}
      roster={roster}
      identities={identities}
      onSave={onSave}
      onDismiss={onDismiss}
      readOnly={readOnly}
    />,
  );
  const written = () => parseSpeakerIdentities(onSave.mock.calls[0][0]);
  return { onSave, onDismiss, written };
};

const nameField = () => screen.getByRole('textbox', { name: 'Name' });
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save' }));

describe('SpeakerAssignmentModal', () => {
  it('names the voice, for every line it spoke, and closes', async () => {
    const { onSave, onDismiss, written } = setup();
    expect(screen.getByText('Speaker: Room 3 (spk_0)')).toBeTruthy();
    expect(screen.getByText(/2 turns/)).toBeTruthy();
    fireEvent.change(nameField(), { target: { value: '  Bob Strahan ' } });
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(written().identities[A]).toMatchObject({ name: 'Bob Strahan', status: 'confirmed', source: 'manual' });
    expect(written().merges).toEqual({});
    await waitFor(() => expect(onDismiss).toHaveBeenCalled());
  });

  it('writes nothing until something has changed', () => {
    const { onSave } = setup();
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true);
    save();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('merges the voice into another on its channel, which then names it', async () => {
    const { onSave, written } = setup();
    const picker = createWrapper(document.body).findSelect();
    picker.openDropdown();
    // The voices on the same channel only: not the local microphone, not itself.
    const offered = picker
      .findDropdown()
      .findOptions()
      .map((option) => option.getElement().textContent);
    expect(offered).toEqual(['No one else (a person of their own)', 'Room 3 (spk_1)', 'Room 3 (spk_2)']);
    picker.selectOptionByValue(B);
    expect(nameField().disabled).toBe(true);
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(written().merges).toEqual({ [A]: B });
    expect(written().identities).toEqual({});
  });

  it('separates a voice merged into this one', async () => {
    const identities = { ...emptyIdentities(), merges: { [B]: A, [C]: A } };
    const { onSave, written } = setup({ identities });
    fireEvent.click(screen.getByRole('checkbox', { name: /^Separate Other Participant \(spk_1\)/ }));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(written().merges).toEqual({ [C]: A });
  });

  it('combines two voices shown under one name into one person', async () => {
    const identities = {
      ...emptyIdentities(),
      identities: { [A]: { name: 'Bob', status: 'confirmed' }, [B]: { name: 'Bob', status: 'confirmed' } },
    };
    const { onSave, written } = setup({ identities, speakerKeys: [A, B], speaker: 'Bob' });
    expect(screen.getByText('2 separate voices are shown as "Bob"')).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: 'They are one person: combine them' }));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(written().merges).toEqual({ [B]: A });
    expect(written().identities[A].name).toBe('Bob');
  });

  it('keeps a locked name as it is', () => {
    const identities = { ...emptyIdentities(), identities: { [A]: { name: 'Bob', locked: true } } };
    setup({ identities, speaker: 'Bob' });
    expect(nameField().disabled).toBe(true);
    expect(screen.getByText(/This name is locked/)).toBeTruthy();
  });

  it('offers nothing to save for a user who may not name speakers', () => {
    const { onSave } = setup({ readOnly: true });
    expect(nameField().disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('stays open and says so when the write fails', async () => {
    const { onDismiss } = setup({ saved: false });
    fireEvent.change(nameField(), { target: { value: 'Bob' } });
    save();
    expect(await screen.findByText('The speaker names could not be saved. Please try again.')).toBeTruthy();
    expect(onDismiss).not.toHaveBeenCalled();
  });
});

describe('applySpeakerAssignment', () => {
  const rows = [
    { key: A, name: '' },
    { key: B, name: '' },
  ];

  it('names each voice when they are not combined', () => {
    const next = applySpeakerAssignment(emptyIdentities(), rows, { name: 'Bob', combine: false, separate: [] });
    expect(Object.keys(next.identities)).toEqual([A, B]);
  });

  it('names only the first once they are combined', () => {
    const next = applySpeakerAssignment(emptyIdentities(), rows, { name: 'Bob', combine: true, separate: [] });
    expect(Object.keys(next.identities)).toEqual([A]);
    expect(next.merges).toEqual({ [B]: A });
  });
});
