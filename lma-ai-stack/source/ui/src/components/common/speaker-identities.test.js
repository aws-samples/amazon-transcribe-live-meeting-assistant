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
  identityKeyFor,
  parseAttendeeList,
  parseSpeakerIdentities,
  resolveMergeTarget,
  resolveSpeaker,
  resolveSpeakerName,
  serializeSpeakerIdentities,
  speakerKey,
} from './speaker-identities';

const withIdentity = (key, entry) => ({ ...emptyIdentities(), identities: { [key]: entry } });

describe('speakerKey', () => {
  it('keeps two voices apart when their ids collide across roster names', () => {
    // Voice ids are numbered per channel and restart at spk_0, so a Virtual
    // Participant meeting names from the roster and can legitimately produce
    // 'Alice (spk_0)' and 'Bob (spk_0)' on the same channel. They are two people.
    expect(speakerKey('CALLER', 'Alice (spk_0)')).not.toBe(speakerKey('CALLER', 'Bob (spk_0)'));
  });

  it('keeps the same voice id apart across channels', () => {
    expect(speakerKey('CALLER', 'Other Participant (spk_0)')).not.toBe(
      speakerKey('AGENT', 'Other Participant (spk_0)'),
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
    const parsed = parseSpeakerIdentities('{"identities":{"CALLER#x":{"name":"Bob"}}}');
    expect(parsed.identities['CALLER#x'].name).toBe('Bob');
    expect(parsed.merges).toEqual({});
    expect(parsed.segmentOverrides).toEqual({});
    expect(parsed.candidates).toEqual([]);
  });

  it('round-trips through serialize', () => {
    const map = { ...emptyIdentities(), candidates: ['Bob'], identities: { 'CALLER#a': { name: 'Bob' } } };
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
  const segment = { channel: 'CALLER', speaker: 'Other Participant (spk_0)', segmentId: 's1' };

  it('returns the mapped name for a named voice', () => {
    const map = withIdentity('CALLER#Other Participant (spk_0)', { name: 'Bob Strahan', status: 'confirmed' });
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
    expect(resolveSpeakerName({ channel: 'CALLER', speaker: 'Other Participant' }, emptyIdentities(), 'Room 3')).toBe(
      'Room 3',
    );
  });

  it('leaves a speaker that already has a real name alone', () => {
    expect(resolveSpeakerName({ channel: 'CALLER', speaker: 'Alice (spk_1)' }, emptyIdentities(), 'Room 3')).toBe(
      'Alice (spk_1)',
    );
  });

  it('reports a name through a merge', () => {
    const map = {
      ...withIdentity('CALLER#Other Participant (spk_0)', { name: 'Bob Strahan' }),
      merges: { 'CALLER#Other Participant (spk_3)': 'CALLER#Other Participant (spk_0)' },
    };
    const split = { channel: 'CALLER', speaker: 'Other Participant (spk_3)' };
    expect(resolveSpeakerName(split, map, 'Someone')).toBe('Bob Strahan');
  });

  it('lets a single-segment override win over the voice name', () => {
    // Diarization does occasionally attribute one turn to the wrong voice, and
    // no label-level rename can fix that one segment.
    const map = {
      ...withIdentity('CALLER#Other Participant (spk_0)', { name: 'Bob Strahan' }),
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
    const map = withIdentity('CALLER#Other Participant (spk_0)', { name: 'Bob', status: 'proposed' });
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
    expect(identityKeyFor({ channel: 'CALLER', speaker: 'Other Participant (spk_0)' }, emptyIdentities())).toBe(
      'CALLER#Other Participant (spk_0)',
    );
  });

  it('is the MERGE TARGET once the voice has been merged', () => {
    // Writing to the segment's own key instead would store an entry that
    // resolution never reads, so the rename would silently do nothing.
    const map = {
      ...emptyIdentities(),
      merges: { 'CALLER#Other Participant (spk_1)': 'CALLER#Other Participant (spk_0)' },
    };
    expect(identityKeyFor({ channel: 'CALLER', speaker: 'Other Participant (spk_1)' }, map)).toBe(
      'CALLER#Other Participant (spk_0)',
    );
  });
});

describe('buildSpeakerRoster', () => {
  const segments = [
    { channel: 'CALLER', speaker: 'Other Participant (spk_0)', startTime: 0, endTime: 4, isPartial: false },
    { channel: 'CALLER', speaker: 'Other Participant (spk_1)', startTime: 5, endTime: 7, isPartial: false },
    { channel: 'CALLER', speaker: 'Other Participant (spk_0)', startTime: 8, endTime: 10, isPartial: false },
    { channel: 'AGENT', speaker: 'Me', startTime: 11, endTime: 12, isPartial: false },
  ];

  it('lists one row per voice with its turn count and talk time', () => {
    const roster = buildSpeakerRoster(segments, emptyIdentities(), 'Room 3');
    expect(roster.map((r) => r.key)).toEqual([
      'CALLER#Other Participant (spk_0)',
      'CALLER#Other Participant (spk_1)',
      'AGENT#Me',
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
      merges: { 'CALLER#Other Participant (spk_1)': 'CALLER#Other Participant (spk_0)' },
    };
    const roster = buildSpeakerRoster(segments, map, 'Room 3');
    expect(roster).toHaveLength(2);
    expect(roster[0].turns).toBe(3);
    expect(roster[0].mergedKeys).toEqual(['CALLER#Other Participant (spk_1)']);
  });

  it('labels a merged row with the target voice, whichever order the segments arrive', () => {
    // Segments are grouped per channel rather than sorted by time, so the
    // merged-AWAY voice can be seen first. The row must still describe the voice
    // the name is stored against.
    const map = {
      ...emptyIdentities(),
      merges: { 'CALLER#Other Participant (spk_1)': 'CALLER#Other Participant (spk_0)' },
    };
    const spk1First = [
      { channel: 'CALLER', speaker: 'Other Participant (spk_1)', startTime: 5, endTime: 7, isPartial: false },
      { channel: 'CALLER', speaker: 'Other Participant (spk_0)', startTime: 0, endTime: 4, isPartial: false },
    ];
    const [merged] = buildSpeakerRoster(spk1First, map, 'Room 3');
    expect(merged.key).toBe('CALLER#Other Participant (spk_0)');
    expect(merged.voiceId).toBe('(spk_0)');
    expect(merged.turns).toBe(2);
  });

  it('skips partials, which carry no voice id yet', () => {
    // Labels arrive only on final results, so a partial would invent a nameless
    // row that then disappears when the final lands.
    const withPartial = [
      ...segments,
      { channel: 'CALLER', speaker: 'Other Participant', startTime: 13, endTime: 14, isPartial: true },
    ];
    expect(buildSpeakerRoster(withPartial, emptyIdentities(), 'Room 3')).toHaveLength(3);
  });

  it('ignores non-human channels', () => {
    const withAssist = [...segments, { channel: 'AGENT_ASSISTANT', speaker: '', startTime: 15, endTime: 16 }];
    expect(buildSpeakerRoster(withAssist, emptyIdentities(), 'Room 3')).toHaveLength(3);
  });

  it('carries an existing name and lock onto the row', () => {
    const map = withIdentity('CALLER#Other Participant (spk_0)', { name: 'Bob', status: 'confirmed', locked: true });
    const roster = buildSpeakerRoster(segments, map, 'Room 3');
    expect(roster[0].name).toBe('Bob');
    expect(roster[0].locked).toBe(true);
  });

  it('handles no segments at all', () => {
    expect(buildSpeakerRoster(undefined, emptyIdentities(), 'Room 3')).toEqual([]);
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
  // Decides whether the Speakers panel starts open: only an UNNAMED voice
  // carrying an engine voice id is work the panel can do something about.
  const unnamed = (channel, voiceId) => ({ channel, voiceId, name: '' });
  const named = (channel, voiceId, name) => ({ channel, voiceId, name });

  it('is true when one channel holds two unnamed voices', () => {
    expect(hasMultipleVoicesOnAChannel([unnamed('CALLER', '(spk_0)'), unnamed('CALLER', '(spk_1)')])).toBe(true);
  });

  it('is false for one voice per channel, however many channels', () => {
    expect(hasMultipleVoicesOnAChannel([unnamed('CALLER', '(spk_0)'), unnamed('AGENT', '(spk_0)')])).toBe(false);
  });

  it('is false once those voices have been named', () => {
    // Re-opening a meeting whose speakers are all named should not reopen the
    // panel as though nothing had been done.
    expect(hasMultipleVoicesOnAChannel([named('CALLER', '(spk_0)', 'Bob'), named('CALLER', '(spk_1)', 'Sarah')])).toBe(
      false,
    );
  });

  it('is false for a Virtual Participant meeting, where voices already read as people', () => {
    // A VP names from the meeting roster, so every row has a real name and no
    // engine voice id. There is nothing to disambiguate.
    const roster = ['Alice', 'Bob', 'Priya'].map((n) => ({ channel: 'CALLER', voiceId: '', name: n }));
    expect(hasMultipleVoicesOnAChannel(roster)).toBe(false);
  });

  it('is false for a single voice and for no voices at all', () => {
    expect(hasMultipleVoicesOnAChannel([unnamed('CALLER', '(spk_0)')])).toBe(false);
    expect(hasMultipleVoicesOnAChannel([])).toBe(false);
    expect(hasMultipleVoicesOnAChannel(undefined)).toBe(false);
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
