/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  buildSpeakerRoster,
  emptyIdentities,
  formatTalkTime,
  hasMultipleVoicesOnAChannel,
  headerSpeakers,
  identityKeyFor,
  parseAttendeeList,
  parseSpeakerIdentities,
  resolveMergeTarget,
  resolveSpeaker,
  resolveSpeakerName,
  serializeSpeakerIdentities,
  speakerDisplayRows,
  speakerGroups,
  speakerKey,
  speakerRowLabel,
  withMerge,
  withoutMerges,
  withSpeakerName,
} from './speaker-identities';

const withIdentity = (key, entry) => ({ ...emptyIdentities(), identities: { [key]: entry } });

describe('speakerKey', () => {
  it('keeps two voices apart when their ids collide across roster names', () => {
    // Voice ids are numbered per channel and restart at spk_0, so a Virtual
    // Participant meeting names from the roster and can legitimately produce
    // 'Alice (spk_0)' and 'Bob (spk_0)' on the same channel. They are two people.
    expect(speakerKey('REMOTE', 'Alice (spk_0)')).not.toBe(speakerKey('REMOTE', 'Bob (spk_0)'));
  });

  it('keeps the same voice id apart across channels', () => {
    expect(speakerKey('REMOTE', 'Other Participant (spk_0)')).not.toBe(
      speakerKey('LOCAL', 'Other Participant (spk_0)'),
    );
  });
});

describe('parseSpeakerIdentities', () => {
  it('returns an empty map for a meeting that predates the feature', () => {
    expect(parseSpeakerIdentities(undefined)).toEqual(emptyIdentities());
    expect(parseSpeakerIdentities(null)).toEqual(emptyIdentities());
    expect(parseSpeakerIdentities('')).toEqual(emptyIdentities());
  });

  it('returns an empty map rather than throwing on unparseable stored JSON', () => {
    // A transcript must still render if the attribute is somehow corrupt.
    expect(parseSpeakerIdentities('{not json')).toEqual(emptyIdentities());
    expect(parseSpeakerIdentities('"a string"')).toEqual(emptyIdentities());
  });

  it('fills in missing sections so every reader can index them', () => {
    const parsed = parseSpeakerIdentities('{"identities":{"REMOTE#x":{"name":"Bob"}}}');
    expect(parsed.identities['REMOTE#x'].name).toBe('Bob');
    expect(parsed.merges).toEqual({});
    expect(parsed.segmentOverrides).toEqual({});
    expect(parsed.candidates).toEqual([]);
  });

  it('round-trips through serialize', () => {
    const map = { ...emptyIdentities(), candidates: ['Bob'], identities: { 'REMOTE#a': { name: 'Bob' } } };
    expect(parseSpeakerIdentities(serializeSpeakerIdentities(map))).toEqual(map);
  });
});

describe('resolveMergeTarget', () => {
  it('follows a multi-step chain', () => {
    // Merging A into B and later B into C is a legitimate sequence of user edits.
    expect(resolveMergeTarget('a', { a: 'b', b: 'c' })).toBe('c');
  });

  it('terminates on a cycle instead of hanging the render', () => {
    expect(resolveMergeTarget('a', { a: 'b', b: 'a' })).toBe('b');
  });
});

describe('resolveSpeaker', () => {
  const segment = { channel: 'REMOTE', speaker: 'Other Participant (spk_0)', segmentId: 's1' };

  it('returns the mapped name for a named voice', () => {
    const map = withIdentity('REMOTE#Other Participant (spk_0)', { name: 'Bob Strahan', status: 'confirmed' });
    expect(resolveSpeaker(segment, map, 'Someone')).toEqual({
      name: 'Bob Strahan',
      status: 'confirmed',
      overridden: false,
      locked: false,
    });
  });

  it('keeps the voice id when the voice has no name', () => {
    // Two voices on one channel differ only by this suffix, so dropping it would
    // merge two people into one apparent speaker.
    expect(resolveSpeakerName(segment, emptyIdentities(), 'Conference Room')).toBe('Conference Room (spk_0)');
  });

  it('preserves the participant name typed on the Stream Audio form', () => {
    expect(resolveSpeakerName({ channel: 'REMOTE', speaker: 'Other Participant' }, emptyIdentities(), 'Room 3')).toBe(
      'Room 3',
    );
  });

  it('leaves a speaker that already has a real name alone', () => {
    expect(resolveSpeakerName({ channel: 'REMOTE', speaker: 'Alice (spk_1)' }, emptyIdentities(), 'Room 3')).toBe(
      'Alice (spk_1)',
    );
  });

  it('reports a name through a merge', () => {
    const map = {
      ...withIdentity('REMOTE#Other Participant (spk_0)', { name: 'Bob Strahan' }),
      merges: { 'REMOTE#Other Participant (spk_3)': 'REMOTE#Other Participant (spk_0)' },
    };
    const split = { channel: 'REMOTE', speaker: 'Other Participant (spk_3)' };
    expect(resolveSpeakerName(split, map, 'Someone')).toBe('Bob Strahan');
  });

  it('lets a single-segment override win over the voice name', () => {
    // Diarization does occasionally attribute one turn to the wrong voice, and
    // no label-level rename can fix that one segment.
    const map = {
      ...withIdentity('REMOTE#Other Participant (spk_0)', { name: 'Bob Strahan' }),
      segmentOverrides: { s1: 'Sarah Lee' },
    };
    expect(resolveSpeaker(segment, map, 'Someone')).toEqual({
      name: 'Sarah Lee',
      status: 'confirmed',
      overridden: true,
      locked: false,
    });
  });

  it('reports an inferred name as proposed so a caller can mark it', () => {
    const map = withIdentity('REMOTE#Other Participant (spk_0)', { name: 'Bob', status: 'proposed' });
    expect(resolveSpeaker(segment, map, 'Someone').status).toBe('proposed');
  });

  it('tolerates a missing map entirely', () => {
    expect(resolveSpeakerName(segment, undefined, 'Someone')).toBe('Someone (spk_0)');
  });

  it('does not read inherited object properties as names', () => {
    // These maps come straight from JSON.parse, so a bare index would return an
    // inherited value for a key like 'constructor' -- which is not a name and
    // which React cannot render.
    const odd = { ...emptyIdentities(), segmentOverrides: {} };
    expect(resolveSpeakerName({ ...segment, segmentId: 'constructor' }, odd, 'Someone')).toBe('Someone (spk_0)');
    expect(resolveMergeTarget('toString', {})).toBe('toString');
  });
});

describe('identityKeyFor', () => {
  it('is the segment key when nothing is merged', () => {
    expect(identityKeyFor({ channel: 'REMOTE', speaker: 'Other Participant (spk_0)' }, emptyIdentities())).toBe(
      'REMOTE#Other Participant (spk_0)',
    );
  });

  it('is the MERGE TARGET once the voice has been merged', () => {
    // Writing to the segment's own key instead would store an entry that
    // resolution never reads, so the rename would silently do nothing.
    const map = {
      ...emptyIdentities(),
      merges: { 'REMOTE#Other Participant (spk_1)': 'REMOTE#Other Participant (spk_0)' },
    };
    expect(identityKeyFor({ channel: 'REMOTE', speaker: 'Other Participant (spk_1)' }, map)).toBe(
      'REMOTE#Other Participant (spk_0)',
    );
  });
});

describe('buildSpeakerRoster', () => {
  const segments = [
    { channel: 'REMOTE', speaker: 'Other Participant (spk_0)', startTime: 0, endTime: 4, isPartial: false },
    { channel: 'REMOTE', speaker: 'Other Participant (spk_1)', startTime: 5, endTime: 7, isPartial: false },
    { channel: 'REMOTE', speaker: 'Other Participant (spk_0)', startTime: 8, endTime: 10, isPartial: false },
    { channel: 'LOCAL', speaker: 'Me', startTime: 11, endTime: 12, isPartial: false },
  ];

  it('lists one row per voice with its turn count and talk time', () => {
    const roster = buildSpeakerRoster(segments, emptyIdentities(), 'Room 3');
    expect(roster.map((r) => r.key)).toEqual([
      'REMOTE#Other Participant (spk_0)',
      'REMOTE#Other Participant (spk_1)',
      'LOCAL#Me',
    ]);
    expect(roster[0].turns).toBe(2);
    expect(roster[0].talkTimeSeconds).toBe(6);
    expect(roster[0].voiceId).toBe('(spk_0)');
    expect(roster[0].defaultName).toBe('Room 3');
  });

  it('orders rows by when each voice was first heard', () => {
    const roster = buildSpeakerRoster([...segments].reverse(), emptyIdentities(), 'Room 3');
    expect(roster.map((r) => r.firstHeardAt)).toEqual([0, 5, 11]);
  });

  it('folds a merged voice into one row', () => {
    const map = {
      ...emptyIdentities(),
      merges: { 'REMOTE#Other Participant (spk_1)': 'REMOTE#Other Participant (spk_0)' },
    };
    const roster = buildSpeakerRoster(segments, map, 'Room 3');
    expect(roster).toHaveLength(2);
    expect(roster[0].turns).toBe(3);
    expect(roster[0].mergedKeys).toEqual(['REMOTE#Other Participant (spk_1)']);
  });

  it('labels a merged row with the target voice, whichever order the segments arrive', () => {
    // Segments are grouped per channel rather than sorted by time, so the
    // merged-AWAY voice can be seen first. The row must still describe the voice
    // the name is stored against.
    const map = {
      ...emptyIdentities(),
      merges: { 'REMOTE#Other Participant (spk_1)': 'REMOTE#Other Participant (spk_0)' },
    };
    const spk1First = [
      { channel: 'REMOTE', speaker: 'Other Participant (spk_1)', startTime: 5, endTime: 7, isPartial: false },
      { channel: 'REMOTE', speaker: 'Other Participant (spk_0)', startTime: 0, endTime: 4, isPartial: false },
    ];
    const [merged] = buildSpeakerRoster(spk1First, map, 'Room 3');
    expect(merged.key).toBe('REMOTE#Other Participant (spk_0)');
    expect(merged.voiceId).toBe('(spk_0)');
    expect(merged.turns).toBe(2);
  });

  it('skips partials, which carry no voice id yet', () => {
    // Labels arrive only on final results, so a partial would invent a nameless
    // row that then disappears when the final lands.
    const withPartial = [
      ...segments,
      { channel: 'REMOTE', speaker: 'Other Participant', startTime: 13, endTime: 14, isPartial: true },
    ];
    expect(buildSpeakerRoster(withPartial, emptyIdentities(), 'Room 3')).toHaveLength(3);
  });

  it('ignores non-human channels', () => {
    const withAssist = [...segments, { channel: 'AGENT_ASSISTANT', speaker: '', startTime: 15, endTime: 16 }];
    expect(buildSpeakerRoster(withAssist, emptyIdentities(), 'Room 3')).toHaveLength(3);
  });

  it('carries an existing name and lock onto the row', () => {
    const map = withIdentity('REMOTE#Other Participant (spk_0)', { name: 'Bob', status: 'confirmed', locked: true });
    const roster = buildSpeakerRoster(segments, map, 'Room 3');
    expect(roster[0].name).toBe('Bob');
    expect(roster[0].locked).toBe(true);
  });

  it('handles no segments at all', () => {
    expect(buildSpeakerRoster(undefined, emptyIdentities(), 'Room 3')).toEqual([]);
  });

  it('treats a custom channel label split into several voices as waiting for names', () => {
    const custom = [
      // With words: as in the resolver, a segment with none is not a voice.
      { channel: 'REMOTE', speaker: 'Others (spk_0)', startTime: 0, endTime: 2, isPartial: false, transcript: 'Hi' },
      { channel: 'REMOTE', speaker: 'Others (spk_1)', startTime: 3, endTime: 4, isPartial: false, transcript: 'Yo' },
      { channel: 'LOCAL', speaker: 'me@example.com', startTime: 5, endTime: 6, isPartial: false, transcript: 'Ok' },
    ];
    const roster = buildSpeakerRoster(custom, emptyIdentities(), 'Others');
    expect(roster.map((r) => [r.key, r.isPlaceholder])).toEqual([
      ['REMOTE#Others (spk_0)', true],
      ['REMOTE#Others (spk_1)', true],
      ['LOCAL#me@example.com', false],
    ]);
    // The display is unchanged: the stored label still shows as itself.
    expect(roster[0].defaultName).toBe('Others');
  });

  it('keeps Virtual Participant roster names out of naming even when they share voice ids', () => {
    const roster = buildSpeakerRoster(
      [
        { channel: 'REMOTE', speaker: 'Alice (spk_0)', startTime: 0, endTime: 2, isPartial: false },
        { channel: 'REMOTE', speaker: 'Alice (spk_1)', startTime: 3, endTime: 4, isPartial: false },
        { channel: 'REMOTE', speaker: 'Bob (spk_1)', startTime: 5, endTime: 6, isPartial: false },
      ],
      emptyIdentities(),
      undefined,
    );
    expect(roster.every((r) => !r.isPlaceholder)).toBe(true);
  });

  it('treats an unseparated default-labelled channel as waiting for a name', () => {
    const roster = buildSpeakerRoster(segments, emptyIdentities(), 'Room 3');
    expect(roster.find((r) => r.key === 'LOCAL#Me').isPlaceholder).toBe(true);
  });

  it('treats the desktop app default label as a placeholder', () => {
    const roster = buildSpeakerRoster(
      [{ channel: 'REMOTE', speaker: 'Other participants (spk_0)', startTime: 0, endTime: 2, isPartial: false }],
      emptyIdentities(),
      undefined,
    );
    expect(roster[0].isPlaceholder).toBe(true);
  });
});

describe('parseAttendeeList', () => {
  it('reads a pasted calendar attendee block', () => {
    const pasted = ['Bob Strahan (Organizer)', 'Sarah Lee', 'Priya Raman (Optional)'].join('\n');
    expect(parseAttendeeList(pasted)).toEqual(['Bob Strahan', 'Sarah Lee', 'Priya Raman']);
  });

  it('reads a comma- or semicolon-separated recipient line', () => {
    expect(parseAttendeeList('Bob Strahan; Sarah Lee, Priya Raman')).toEqual([
      'Bob Strahan',
      'Sarah Lee',
      'Priya Raman',
    ]);
  });

  it('prefers the display name over the address', () => {
    expect(parseAttendeeList('Bob Strahan <bob@example.com>')).toEqual(['Bob Strahan']);
  });

  it('derives a readable name from a bare address', () => {
    expect(parseAttendeeList('bob.strahan@example.com')).toEqual(['Bob Strahan']);
    expect(parseAttendeeList('<sarah_lee@example.com>')).toEqual(['Sarah Lee']);
  });

  it('de-duplicates case-insensitively, keeping the first spelling', () => {
    expect(parseAttendeeList('Bob Strahan\nbob strahan')).toEqual(['Bob Strahan']);
  });

  it('drops noise rather than producing junk candidates', () => {
    expect(parseAttendeeList('')).toEqual([]);
    expect(parseAttendeeList(undefined)).toEqual([]);
    // A single character is never a name, and the autocomplete is unusable if
    // stray punctuation becomes a candidate.
    expect(parseAttendeeList('a\n-\n,,\n')).toEqual([]);
  });

  it('keeps a quoted display name whole even though it contains a comma', () => {
    // Splitting on the comma before extracting the bracketed entry would turn
    // one attendee into two half-names.
    expect(parseAttendeeList('"Lee, Sarah" <sarah@example.com>')).toEqual(['Lee, Sarah']);
  });

  it('reads a mixed line of bracketed and bare entries', () => {
    const pasted = '"Lee, Sarah" <sarah@example.com>, Bob Strahan <bob@example.com>; Priya Raman';
    expect(parseAttendeeList(pasted).sort()).toEqual(['Bob Strahan', 'Lee, Sarah', 'Priya Raman']);
  });
});

describe('hasMultipleVoicesOnAChannel', () => {
  // Decides whether the Speakers panel starts open. Rows are built through
  // buildSpeakerRoster rather than hand-made, so this cannot pass against a row
  // shape the roster never actually produces -- which is how an earlier version
  // of these tests passed for the wrong reason.
  const seg = (speaker, start_, channel = 'REMOTE') => ({
    channel,
    speaker,
    startTime: start_,
    endTime: start_ + 3,
    isPartial: false,
  });
  const roster = (segments, identities = emptyIdentities()) => buildSpeakerRoster(segments, identities, 'Room 3');

  it('is true when one channel holds two unnamed placeholder voices', () => {
    expect(
      hasMultipleVoicesOnAChannel(roster([seg('Other Participant (spk_0)', 0), seg('Other Participant (spk_1)', 5)])),
    ).toBe(true);
  });

  it('is false for one voice per channel, however many channels', () => {
    expect(
      hasMultipleVoicesOnAChannel(
        roster([seg('Other Participant (spk_0)', 0), seg('Other Participant (spk_0)', 5, 'LOCAL')]),
      ),
    ).toBe(false);
  });

  it('is false once those voices have been named', () => {
    // Re-opening a meeting whose speakers are all named should not reopen the
    // panel as though nothing had been done.
    const named = {
      ...emptyIdentities(),
      identities: {
        'REMOTE#Other Participant (spk_0)': { name: 'Bob' },
        'REMOTE#Other Participant (spk_1)': { name: 'Sarah' },
      },
    };
    expect(
      hasMultipleVoicesOnAChannel(
        roster([seg('Other Participant (spk_0)', 0), seg('Other Participant (spk_1)', 5)], named),
      ),
    ).toBe(false);
  });

  it('is false for a Virtual Participant meeting, whose voices carry roster names', () => {
    // A VP names from the meeting roster, so the stored speaker already reads as
    // a person -- but it STILL carries an engine voice id, which is why the voice
    // id alone cannot be the test.
    const vp = roster([seg('Alice (spk_0)', 0), seg('Bob (spk_0)', 5), seg('Priya (spk_1)', 10)]);
    expect(vp.every((row) => row.voiceId)).toBe(true);
    expect(hasMultipleVoicesOnAChannel(vp)).toBe(false);
  });

  it('is TRUE when a client-supplied channel name was split into two voices', () => {
    // 'alex@example.com (spk_0)' and '(spk_1)' is two real people behind one
    // tab. They need naming just as much as two placeholder voices do, even
    // though the base name already looks like a person.
    expect(
      hasMultipleVoicesOnAChannel(roster([seg('alex@example.com (spk_0)', 0), seg('alex@example.com (spk_1)', 5)])),
    ).toBe(true);
  });

  it('is false when two voices on a channel are different people already', () => {
    // A Virtual Participant meeting: distinct base names, so nothing is split.
    expect(hasMultipleVoicesOnAChannel(roster([seg('Alice (spk_0)', 0), seg('Bob (spk_0)', 5)]))).toBe(false);
  });

  describe('by the meeting record CaptureSource', () => {
    const split = [seg('Bob Strahan (spk_0)', 0), seg('Bob Strahan (spk_1)', 5)];
    const vpRoster = (segments) =>
      buildSpeakerRoster(segments, emptyIdentities(), 'Room 3', { captureSource: 'VIRTUAL_PARTICIPANT' });
    const groupsOf = (segments) => speakerGroups(segments, emptyIdentities());

    it("is false for a Virtual Participant meeting's one attendee split into two voices", () => {
      const rows = vpRoster(split);
      expect(hasMultipleVoicesOnAChannel(rows, { captureSource: 'VIRTUAL_PARTICIPANT', groups: groupsOf(split) })).toBe(
        false,
      );
    });

    it("is true for a Virtual Participant meeting's room, whose voices are shown as several people", () => {
      const room = [seg('Conference Room B (spk_0)', 0), seg('Conference Room B (spk_1)', 5)];
      expect(
        hasMultipleVoicesOnAChannel(vpRoster(room), { captureSource: 'VIRTUAL_PARTICIPANT', groups: groupsOf(room) }),
      ).toBe(true);
    });

    it('is true for a Virtual Participant meeting whose placeholder voices need names', () => {
      const unknown = [seg('Unknown (spk_0)', 0), seg('Unknown (spk_1)', 5)];
      expect(
        hasMultipleVoicesOnAChannel(vpRoster(unknown), {
          captureSource: 'VIRTUAL_PARTICIPANT',
          groups: groupsOf(unknown),
        }),
      ).toBe(true);
    });

    it("is still true for a Stream Audio channel's typed label split into two voices", () => {
      const typed = [seg('alex@example.com (spk_0)', 0), seg('alex@example.com (spk_1)', 5)];
      const rows = buildSpeakerRoster(typed, emptyIdentities(), 'Room 3', { captureSource: 'STREAM_AUDIO' });
      expect(hasMultipleVoicesOnAChannel(rows, { captureSource: 'STREAM_AUDIO', groups: groupsOf(typed) })).toBe(true);
    });

    it('keeps the structural rule for a meeting with no CaptureSource', () => {
      const rows = roster(split);
      expect(hasMultipleVoicesOnAChannel(rows, { groups: groupsOf(split) })).toBe(true);
    });

    it("marks the split name's voices as waiting for a name only where it can be a channel label", () => {
      const spoken = split.map((s) => ({ ...s, transcript: 'words' }));
      const placeholders = (options) =>
        buildSpeakerRoster(spoken, emptyIdentities(), 'Room 3', options).map((row) => row.isPlaceholder);
      expect(placeholders({ captureSource: 'VIRTUAL_PARTICIPANT' })).toEqual([false, false]);
      expect(placeholders({ captureSource: 'STREAM_AUDIO' })).toEqual([true, true]);
      expect(placeholders(undefined)).toEqual([true, true]);
    });
  });

  it('is false for a single voice and for no voices at all', () => {
    expect(hasMultipleVoicesOnAChannel(roster([seg('Other Participant (spk_0)', 0)]))).toBe(false);
    expect(hasMultipleVoicesOnAChannel([])).toBe(false);
    expect(hasMultipleVoicesOnAChannel(undefined)).toBe(false);
  });
});

describe('buildSpeakerRoster channel scoping', () => {
  it('does not offer the remote participant name as a local-channel placeholder', () => {
    // The Stream Audio form's name is the remote side's. Using it for an
    // LOCAL-channel voice would make the panel's placeholder disagree with the
    // transcript beside it, which shows the generic placeholder there.
    const rows = buildSpeakerRoster(
      [
        { channel: 'REMOTE', speaker: 'Other Participant', startTime: 0, endTime: 3, isPartial: false },
        { channel: 'LOCAL', speaker: 'Other Participant', startTime: 5, endTime: 8, isPartial: false },
      ],
      emptyIdentities(),
      'Conference Room 3',
    );
    expect(rows.find((r) => r.channel === 'REMOTE').defaultName).toBe('Conference Room 3');
    expect(rows.find((r) => r.channel === 'LOCAL').defaultName).toBe('Other Participant');
  });
});

describe('formatTalkTime', () => {
  it('formats as mm:ss', () => {
    expect(formatTalkTime(0)).toBe('0:00');
    expect(formatTalkTime(9)).toBe('0:09');
    expect(formatTalkTime(125)).toBe('2:05');
  });

  it('tolerates a missing total', () => {
    expect(formatTalkTime(undefined)).toBe('0:00');
  });
});

describe('a meeting stored under the older channel names', () => {
  // LMA before 0.4.0 wrote the speech channels as CALLER and AGENT and keyed the
  // identity map by them. Such a map must still name the meeting's voices.
  const stored = JSON.stringify({
    version: 1,
    identities: {
      'CALLER#Other Participant (spk_0)': { name: 'Bob', status: 'confirmed' },
      'AGENT#Me': { name: 'Sam', status: 'confirmed' },
    },
    merges: { 'CALLER#Other Participant (spk_1)': 'CALLER#Other Participant (spk_0)' },
    rejected: { 'AGENT#Me': ['Bobby'] },
    segmentOverrides: { s9: 'Ann' },
  });

  it('is re-keyed under REMOTE and LOCAL when parsed', () => {
    const parsed = parseSpeakerIdentities(stored);
    expect(parsed.identities).toEqual({
      'REMOTE#Other Participant (spk_0)': { name: 'Bob', status: 'confirmed' },
      'LOCAL#Me': { name: 'Sam', status: 'confirmed' },
    });
    expect(parsed.merges).toEqual({ 'REMOTE#Other Participant (spk_1)': 'REMOTE#Other Participant (spk_0)' });
    expect(parsed.rejected).toEqual({ 'LOCAL#Me': ['Bobby'] });
    expect(parsed.segmentOverrides).toEqual({ s9: 'Ann' });
  });

  it('names segments stored under either spelling of the channel', () => {
    const parsed = parseSpeakerIdentities(stored);
    expect(resolveSpeakerName({ channel: 'CALLER', speaker: 'Other Participant (spk_0)' }, parsed)).toBe('Bob');
    expect(resolveSpeakerName({ channel: 'REMOTE', speaker: 'Other Participant (spk_1)' }, parsed)).toBe('Bob');
    expect(resolveSpeakerName({ channel: 'AGENT', speaker: 'Me' }, parsed)).toBe('Sam');
  });

  it('keys a voice the same way under either spelling', () => {
    expect(speakerKey('CALLER', 'Alice (spk_0)')).toBe(speakerKey('REMOTE', 'Alice (spk_0)'));
    expect(speakerKey('AGENT', 'Me')).toBe('LOCAL#Me');
  });

  it('lists a voice heard under both spellings as one row on the current channel', () => {
    const roster = buildSpeakerRoster(
      [
        { channel: 'CALLER', speaker: 'Other Participant (spk_0)', segmentId: 'a', startTime: 0, endTime: 2 },
        { channel: 'REMOTE', speaker: 'Other Participant (spk_0)', segmentId: 'b', startTime: 3, endTime: 4 },
      ],
      parseSpeakerIdentities(stored),
      'Dana',
    );
    expect(roster).toHaveLength(1);
    expect(roster[0]).toMatchObject({ channel: 'REMOTE', name: 'Bob', turns: 2 });
  });
});

describe('the edits the Speakers panel and the speaker modal make', () => {
  const A = 'REMOTE#Other Participant (spk_0)';
  const B = 'REMOTE#Other Participant (spk_1)';

  it('names a voice as a person would: confirmed and manual, keeping what else it held', () => {
    const start = withIdentity(A, { name: 'Bob', status: 'proposed', locked: false, evidence: { quote: 'hi' } });
    const next = withSpeakerName(start, A, 'Bob Strahan', '2026-11-05T10:00:00Z');
    expect(next.identities[A]).toEqual({
      name: 'Bob Strahan',
      status: 'confirmed',
      source: 'manual',
      updatedAt: '2026-11-05T10:00:00Z',
      locked: false,
      evidence: { quote: 'hi' },
    });
    expect(start.identities[A].name).toBe('Bob');
  });

  it('removes the entry for an empty name, so the voice id shows again', () => {
    expect(withSpeakerName(withIdentity(A, { name: 'Bob' }), A, '').identities).toEqual({});
  });

  it('merges a voice into another, and separates it again', () => {
    const merged = withMerge(emptyIdentities(), B, A);
    expect(merged.merges).toEqual({ [B]: A });
    expect(
      resolveSpeakerName(
        { channel: 'REMOTE', speaker: 'Other Participant (spk_1)' },
        withSpeakerName(merged, A, 'Bob'),
      ),
    ).toBe('Bob');
    expect(withoutMerges(merged, [B]).merges).toEqual({});
    expect(merged.merges).toEqual({ [B]: A });
  });

  it('labels a roster row by its name, or by its placeholder and voice id', () => {
    expect(speakerRowLabel({ name: 'Bob', defaultName: 'Room 3', voiceId: '(spk_0)' })).toBe('Bob');
    expect(speakerRowLabel({ name: '', defaultName: 'Room 3', voiceId: '(spk_0)' })).toBe('Room 3 (spk_0)');
    expect(speakerRowLabel({ name: '', defaultName: 'Me', voiceId: '' })).toBe('Me');
  });
});

describe('headerSpeakers', () => {
  const line = (speaker, startTime, endTime, channel = 'REMOTE') => ({
    channel,
    speaker,
    startTime,
    endTime,
    isPartial: false,
    transcript: 'Words.',
  });
  const SEGMENTS = [
    line('Bob Strahan (spk_0)', 0, 100),
    line('Bob Strahan (spk_1)', 100, 101),
    line('ConfRm_1 (spk_0)', 101, 110),
    line('ConfRm_1 (spk_1)', 110, 120),
    line('Other Participant (spk_0)', 120, 125),
  ];
  const header = (map, fallbackName = undefined) => {
    const identities = parseSpeakerIdentities(map);
    const roster = buildSpeakerRoster(SEGMENTS, identities, fallbackName);
    return headerSpeakers(speakerDisplayRows(roster, identities, speakerGroups(SEGMENTS, identities), fallbackName));
  };

  it('names each speaker the transcript shows once, in the order first heard', () => {
    expect(header(undefined).map((s) => s.label)).toEqual([
      'Bob Strahan',
      'ConfRm_1 · Voice 1',
      'ConfRm_1 · Voice 2',
      'Other Participant (spk_0)',
    ]);
  });

  it("keeps every voice of a name shown as one person under the name's one entry", () => {
    const [bob] = header(undefined);
    expect(bob).toEqual({
      key: 'REMOTE#Bob Strahan (spk_0)',
      label: 'Bob Strahan',
      keys: ['REMOTE#Bob Strahan (spk_0)', 'REMOTE#Bob Strahan (spk_1)'],
    });
  });

  it('follows a chosen mode and the names given to voices', () => {
    const map = {
      identities: { 'REMOTE#ConfRm_1 (spk_1)': { name: 'Priya', status: 'confirmed' } },
      speakerModes: { 'REMOTE#Bob Strahan': { mode: 'several' }, 'REMOTE#ConfRm_1': { mode: 'one' } },
    };
    expect(header(map).map((s) => s.label)).toEqual([
      'Bob Strahan · Voice 1',
      'Bob Strahan · Voice 2',
      'Priya',
      'Other Participant (spk_0)',
    ]);
  });

  it('substitutes the Stream Audio participant name for the remote placeholder, as the transcript does', () => {
    expect(header(undefined, 'Sam Ortiz').map((s) => s.label)).toContain('Sam Ortiz (spk_0)');
  });
});
