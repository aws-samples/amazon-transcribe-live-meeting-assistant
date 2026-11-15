/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
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
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

import createWrapper from '@cloudscape-design/components/test-utils/dom';

import SpeakerIdentitiesPanel from './SpeakerIdentitiesPanel';

/* eslint-disable react/prop-types */

const SEGMENTS = [
  {
    channel: 'REMOTE',
    speaker: 'Other Participant (spk_0)',
    startTime: 0,
    endTime: 4,
    isPartial: false,
    segmentId: 's1',
    transcript: 'Shall we start?',
  },
  {
    channel: 'REMOTE',
    speaker: 'Other Participant (spk_0)',
    startTime: 9,
    endTime: 11,
    isPartial: false,
    segmentId: 's2',
    transcript: 'Next item.',
  },
  {
    channel: 'REMOTE',
    speaker: 'Other Participant (spk_1)',
    startTime: 5,
    endTime: 7,
    isPartial: false,
    segmentId: 's3',
    transcript: 'Sounds good.',
  },
];

const ITEM = { callId: 'c1', remoteParticipantName: 'Conference Room 3', speakerIdentities: undefined };

let onSave;

let onIdentify;

const renderPanel = ({
  item = ITEM,
  segments = SEGMENTS,
  readOnly = false,
  identifyResult = { Success: true, NamesApplied: 2 },
  isLive = false,
  focusRequest = null,
} = {}) => {
  onSave = vi.fn().mockResolvedValue(true);
  onIdentify = vi.fn().mockResolvedValue(identifyResult);
  return render(
    <SpeakerIdentitiesPanel
      item={item}
      segments={segments}
      setToolsOpen={() => {}}
      onSave={onSave}
      onIdentify={onIdentify}
      readOnly={readOnly}
      isLive={isLive}
      focusRequest={focusRequest}
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
    expect(map.identities['REMOTE#Other Participant (spk_0)']).toMatchObject({
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
    expect(savedMap().identities['REMOTE#Other Participant (spk_0)'].status).toBe('confirmed');
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
          identities: { 'REMOTE#Other Participant (spk_0)': { name: 'Bob' } },
        }),
      },
    });
    const field = screen.getByDisplayValue('Bob');
    fireEvent.change(field, { target: { value: '' } });
    fireEvent.blur(field);

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(savedMap().identities['REMOTE#Other Participant (spk_0)']).toBeUndefined();
  });

  it('keeps an existing name in the field so it can be edited', () => {
    renderPanel({
      item: {
        ...ITEM,
        speakerIdentities: JSON.stringify({
          identities: { 'REMOTE#Other Participant (spk_0)': { name: 'Bob' } },
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
          merges: { 'REMOTE#Other Participant (spk_1)': 'REMOTE#Other Participant (spk_0)' },
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
    expect(savedMap().identities['REMOTE#Other Participant (spk_0)'].name).toBe('Someone Unlisted');
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
      'speakerModes',
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
              'REMOTE#Other Participant (spk_0)': { name: 'Bob', status: 'confirmed' },
              'REMOTE#Other Participant (spk_1)': { name: 'Sarah', status: 'confirmed' },
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
              'REMOTE#Other Participant (spk_0)': { name: 'Bob', status: 'proposed' },
              'REMOTE#Other Participant (spk_1)': { name: 'Sarah', status: 'proposed' },
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

describe('what the resolver answered, and when automatic passes stop', () => {
  const ROSTER = [
    { channel: 'REMOTE', speaker: 'Bob Strahan (spk_0)', startTime: 0, endTime: 4, transcript: 'Hi', segmentId: 'r1' },
    { channel: 'REMOTE', speaker: 'Bob Strahan (spk_1)', startTime: 5, endTime: 6, transcript: 'ok', segmentId: 'r2' },
    { channel: 'REMOTE', speaker: 'Chris Lott (spk_0)', startTime: 7, endTime: 9, transcript: 'Yo', segmentId: 'r3' },
  ];

  it('says the voices are named by the platform, and points at the toggle', async () => {
    renderPanel({ segments: ROSTER, identifyResult: { Success: true, NamesApplied: 0, Reason: 'ALREADY_NAMED' } });
    fireEvent.click(screen.getByText('Identify speakers').closest('button'));
    await waitFor(() =>
      expect(screen.getByText(/Every voice already has a name from the meeting platform/)).toBeTruthy(),
    );
    expect(screen.getByText(/one-person \/ several-people icon/)).toBeTruthy();
    expect(screen.queryByText(/Nothing in the transcript names them yet/)).toBeNull();
  });

  it('keeps "nothing names them" for a transcript that names nobody', async () => {
    renderPanel({ identifyResult: { Success: true, NamesApplied: 0, Reason: 'NO_EVIDENCE' } });
    fireEvent.click(screen.getByText('Identify speakers').closest('button'));
    await waitFor(() => expect(screen.getByText(/Nothing in the transcript names them yet/)).toBeTruthy());
  });

  it('runs no automatic pass on a meeting whose voices all carry roster names', () => {
    vi.useFakeTimers();
    try {
      renderPanel({ isLive: true, segments: ROSTER });
      vi.advanceTimersByTime(300000);
      expect(onIdentify).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  describe("one attendee split into two voices, by the meeting record's CaptureSource", () => {
    const ONE_ATTENDEE = ROSTER.slice(0, 2);
    const expandedOf = (container) =>
      container.querySelector('[aria-expanded]')?.getAttribute('aria-expanded') === 'true';

    it('on a Virtual Participant meeting: no automatic pass, and the panel starts closed', () => {
      vi.useFakeTimers();
      try {
        const { container } = renderPanel({
          isLive: true,
          segments: ONE_ATTENDEE,
          item: { ...ITEM, captureSource: 'VIRTUAL_PARTICIPANT' },
        });
        expect(expandedOf(container)).toBe(false);
        vi.advanceTimersByTime(300000);
        expect(onIdentify).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it('with no CaptureSource: read as the channel label, as before', async () => {
      vi.useFakeTimers();
      try {
        const { container } = renderPanel({ isLive: true, segments: ONE_ATTENDEE });
        expect(expandedOf(container)).toBe(true);
        await vi.advanceTimersByTimeAsync(46000);
        expect(onIdentify).toHaveBeenCalledWith({ force: false });
      } finally {
        vi.useRealTimers();
      }
    });
  });

  it('stops for good when the resolver says nothing is left to name', async () => {
    vi.useFakeTimers();
    try {
      renderPanel({ isLive: true, identifyResult: { Success: true, NamesApplied: 0, Reason: 'ALREADY_NAMED' } });
      await vi.advanceTimersByTimeAsync(46000);
      expect(onIdentify).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(600000);
      expect(onIdentify).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops, and has the meeting read again, when the resolver says it has ended', async () => {
    vi.useFakeTimers();
    try {
      onSave = vi.fn();
      onIdentify = vi.fn().mockResolvedValue({ Success: true, NamesApplied: 0, Reason: 'MEETING_ENDED' });
      const onMeetingEnded = vi.fn();
      render(
        <SpeakerIdentitiesPanel
          item={ITEM}
          segments={SEGMENTS}
          setToolsOpen={() => {}}
          onSave={onSave}
          onIdentify={onIdentify}
          readOnly={false}
          isLive
          onMeetingEnded={onMeetingEnded}
        />,
      );
      await vi.advanceTimersByTimeAsync(46000);
      expect(onMeetingEnded).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(600000);
      expect(onIdentify).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('waits after a pass that found nothing until somebody speaks', async () => {
    vi.useFakeTimers();
    try {
      onSave = vi.fn();
      onIdentify = vi.fn().mockResolvedValue({ Success: true, NamesApplied: 0, Reason: 'NO_EVIDENCE' });
      const panel = (segments) => (
        <SpeakerIdentitiesPanel
          item={ITEM}
          segments={segments}
          setToolsOpen={() => {}}
          onSave={onSave}
          onIdentify={onIdentify}
          readOnly={false}
          isLive
        />
      );
      const { rerender } = render(panel(SEGMENTS));
      await vi.advanceTimersByTimeAsync(46000);
      expect(onIdentify).toHaveBeenCalledTimes(1);
      // A tab whose transcript is not growing -- a quiet meeting, or one whose
      // subscription dropped -- asks no more.
      await vi.advanceTimersByTimeAsync(600000);
      expect(onIdentify).toHaveBeenCalledTimes(1);
      rerender(
        panel([...SEGMENTS, { ...SEGMENTS[0], segmentId: 's4', startTime: 20, endTime: 22, transcript: 'I am Sarah' }]),
      );
      await vi.advanceTimersByTimeAsync(46000);
      expect(onIdentify).toHaveBeenCalledTimes(2);
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
        'REMOTE#Other Participant (spk_0)': {
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
    const entry = savedMap().identities['REMOTE#Other Participant (spk_0)'];
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
    expect(map.identities['REMOTE#Other Participant (spk_0)']).toBeUndefined();
    expect(map.rejected['REMOTE#Other Participant (spk_0)']).toEqual(['Bob Strahan']);
  });
});

describe("a platform name's voices, shown as one person or several", () => {
  // A Virtual Participant meeting: Jeremy's voice split in two, and a room with two people in it.
  const line = (id, speaker, startTime, endTime) => ({
    channel: 'REMOTE',
    speaker,
    startTime,
    endTime,
    isPartial: false,
    segmentId: id,
    transcript: 'Words.',
  });
  const VOICES = [
    line('j1', 'Jeremy Feldman (spk_0)', 0, 100),
    line('j2', 'Jeremy Feldman (spk_1)', 100, 102),
    line('r1', 'ConfRm_1 (spk_0)', 102, 110),
    line('r2', 'ConfRm_1 (spk_1)', 110, 120),
  ];
  const withMap = (map) => ({ ...ITEM, speakerIdentities: JSON.stringify(map) });
  const groupOf = (base) => screen.getByRole('group', { name: `Voices of ${base}` });

  it("lists a name's voices together under its one-person / several-people toggle, as Voice N", () => {
    renderPanel({ segments: VOICES });
    const jeremy = groupOf('Jeremy Feldman');
    expect(within(jeremy).getByText('Voice 1')).toBeTruthy();
    expect(within(jeremy).getByText('Voice 2')).toBeTruthy();
    const toggleOf = (base) => within(groupOf(base)).getByRole('button', { name: `Show ${base} as several people` });
    expect(toggleOf('Jeremy Feldman').getAttribute('aria-pressed')).toBe('false');
    expect(toggleOf('ConfRm_1').getAttribute('aria-pressed')).toBe('true');
  });

  it('shows each voice of a name shown as several people by the label its lines carry', () => {
    renderPanel({ segments: VOICES });
    const room = groupOf('ConfRm_1');
    const [first, second] = within(room).getAllByRole('textbox');
    expect(first.getAttribute('placeholder')).toBe('ConfRm_1 · Voice 1');
    expect(second.getAttribute('placeholder')).toBe('ConfRm_1 · Voice 2');
  });

  it('stores the mode chosen in the panel', async () => {
    renderPanel({ segments: VOICES });
    fireEvent.click(
      within(groupOf('Jeremy Feldman')).getByRole('button', { name: 'Show Jeremy Feldman as several people' }),
    );
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(savedMap().speakerModes['REMOTE#Jeremy Feldman']).toMatchObject({ mode: 'several', source: 'manual' });
  });

  it('naming one voice of a name shown as one person names all its voices', async () => {
    renderPanel({ segments: VOICES });
    const [first] = within(groupOf('Jeremy Feldman')).getAllByRole('textbox');
    fireEvent.change(first, { target: { value: 'Jeremy F.' } });
    fireEvent.blur(first);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const { identities } = savedMap();
    expect(identities['REMOTE#Jeremy Feldman (spk_0)']).toMatchObject({ name: 'Jeremy F.', source: 'manual' });
    expect(identities['REMOTE#Jeremy Feldman (spk_1)']).toMatchObject({ name: 'Jeremy F.', source: 'manual' });
  });

  it('naming a voice of a name shown as several people names that voice alone', async () => {
    renderPanel({ segments: VOICES });
    const [, second] = within(groupOf('ConfRm_1')).getAllByRole('textbox');
    fireEvent.change(second, { target: { value: 'Priya' } });
    fireEvent.keyDown(second, { key: 'Enter', keyCode: 13 });
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const { identities } = savedMap();
    expect(identities['REMOTE#ConfRm_1 (spk_1)']).toMatchObject({ name: 'Priya' });
    expect(identities['REMOTE#ConfRm_1 (spk_0)']).toBeUndefined();
  });

  it('a renamed voice keeps its row under the name, holding the new name', () => {
    renderPanel({
      segments: VOICES,
      item: withMap({ identities: { 'REMOTE#ConfRm_1 (spk_1)': { name: 'Priya', status: 'confirmed' } } }),
    });
    const [, second] = within(groupOf('ConfRm_1')).getAllByRole('textbox');
    expect(second.value).toBe('Priya');
  });

  it('opens at the voice the meeting header asked for', () => {
    // Collapsed to begin with: no channel has two unnamed voices under one placeholder.
    renderPanel({ segments: VOICES, focusRequest: { key: 'REMOTE#ConfRm_1 (spk_1)', at: 1 } });
    const row = document.querySelector('[data-speaker-key="REMOTE#ConfRm_1 (spk_1)"]');
    expect(row.className).toBe('speaker-row-focused');
    expect(row.contains(document.activeElement)).toBe(true);
  });

  it('offers the toggle disabled to a reader who may not name speakers', () => {
    renderPanel({ segments: VOICES, readOnly: true });
    expect(within(groupOf('ConfRm_1')).getByRole('button', { name: 'Show ConfRm_1 as several people' }).disabled).toBe(
      true,
    );
  });
});

describe('a Virtual Participant meeting whose attendees have one voice label each', () => {
  // A Zoom stand-up with voice separation on; one of Bob's lines was named by the
  // meeting page before his voice label was attributed to him, so it carries his
  // bare name.
  const line = (id, speaker, startTime, endTime) => ({
    channel: 'REMOTE',
    speaker,
    startTime,
    endTime,
    isPartial: false,
    segmentId: id,
    transcript: 'Words.',
  });
  const STANDUP = [
    line('s0', 'Chris Lott (spk_0)', 1, 2),
    line('s1', 'Jeremy [C] Feldman (spk_1)', 5, 10),
    line('s2', 'Bob Strahan (spk_2)', 11, 18),
    line('s9', 'Bob Strahan', 81, 82),
    line('s10', 'Bob Strahan (spk_2)', 83, 90),
  ];
  const VP_ITEM = { ...ITEM, remoteParticipantName: 'Virtual Participant', captureSource: 'VIRTUAL_PARTICIPANT' };
  const groupOf = (base) => screen.getByRole('group', { name: `Voices of ${base}` });

  it('lists each attendee with an enabled one-person / several-people toggle', () => {
    renderPanel({ item: VP_ITEM, segments: STANDUP });
    ['Chris Lott', 'Jeremy [C] Feldman', 'Bob Strahan'].forEach((name) => {
      const toggle = within(groupOf(name)).getByRole('button', { name: `Show ${name} as several people` });
      expect(toggle.disabled).toBe(false);
      expect(toggle.getAttribute('aria-pressed')).toBe('false');
    });
  });

  it("lists a name's unlabelled lines with its voice, as one voice", () => {
    renderPanel({ item: VP_ITEM, segments: STANDUP });
    const bob = groupOf('Bob Strahan');
    expect(within(bob).getByText('1 voice')).toBeTruthy();
    expect(within(bob).getAllByRole('textbox')).toHaveLength(1);
    // Three turns: both labelled lines and the unlabelled one.
    expect(within(bob).getByText(/^3 turns/)).toBeTruthy();
    expect(screen.getByText('(0/3)')).toBeTruthy();
  });

  it('offers each other attendee once, by name, to merge into', () => {
    renderPanel({ item: VP_ITEM, segments: STANDUP });
    const picker = createWrapper(groupOf('Chris Lott')).findSelect();
    picker.openDropdown();
    const offered = picker
      .findDropdown()
      .findOptions()
      .map((option) => option.getElement().textContent);
    expect(offered).toEqual(['Same person as…', 'Jeremy [C] Feldman', 'Bob Strahan']);
  });

  it("merges a name's unlabelled lines along with its voice", async () => {
    renderPanel({ item: VP_ITEM, segments: STANDUP });
    const picker = createWrapper(groupOf('Bob Strahan')).findSelect();
    picker.openDropdown();
    picker.selectOptionByValue('REMOTE#Chris Lott (spk_0)');
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(savedMap().merges).toEqual({
      'REMOTE#Bob Strahan (spk_2)': 'REMOTE#Chris Lott (spk_0)',
      'REMOTE#Bob Strahan': 'REMOTE#Chris Lott (spk_0)',
    });
  });

  it('opens at the voice holding the unlabelled lines the meeting header asked for', () => {
    renderPanel({ item: VP_ITEM, segments: STANDUP, focusRequest: { key: 'REMOTE#Bob Strahan', at: 1 } });
    const row = document.querySelector('[data-speaker-key="REMOTE#Bob Strahan (spk_2)"]');
    expect(row.className).toBe('speaker-row-focused');
  });
});
