/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

import {
  nameableVoiceKeys,
  parseSpeakerIdentities,
  resolveSpeakerName,
  serializeSpeakerIdentities,
} from './speaker-identities';
import {
  describeSpeakerMode,
  isUnknownName,
  looksLikeRoom,
  ONE_PERSON,
  SEVERAL_PEOPLE,
  speakerGroups,
  speakerModeTip,
  withSpeakerMode,
} from './speaker-modes';

const FIXTURES = path.join(__dirname, '../../../../../../test-fixtures');
// The same tables lma_speakers and the speaker-name resolver are tested against.
const TABLE = JSON.parse(readFileSync(path.join(FIXTURES, 'speaker-modes.json'), 'utf-8'));
const NAMEABLE = JSON.parse(readFileSync(path.join(FIXTURES, 'nameable-voices.json'), 'utf-8'));

const segmentsOf = (c) =>
  c.segments.map((s, index) => ({
    channel: s.channel,
    speaker: s.speaker,
    startTime: s.start,
    endTime: s.end ?? s.start + 1,
    isPartial: s.isPartial ?? false,
    segmentId: s.segmentId ?? `seg-${index}`,
    transcript: s.transcript ?? 'words',
  }));

const identitiesOf = (c) =>
  parseSpeakerIdentities(
    c.identities && typeof c.identities === 'object' ? JSON.stringify(c.identities) : c.identities,
  );

describe('speaker modes, by the shared table', () => {
  TABLE.cases.forEach((c) => {
    it(`${c.name}: names`, () => {
      const segments = segmentsOf(c);
      const identities = identitiesOf(c);
      const groups = speakerGroups(segments, identities);
      // The UI passes the Stream Audio name for the REMOTE channel only; the
      // Python resolver applies the same scope itself.
      const names = segments.map((s) =>
        resolveSpeakerName(s, identities, s.channel === 'REMOTE' ? c.fallbackName : undefined, groups),
      );
      expect(names).toEqual(c.names);
    });

    it(`${c.name}: groups`, () => {
      const groups = speakerGroups(segmentsOf(c), identitiesOf(c));
      const got = Object.fromEntries(
        Object.entries(groups).map(([key, g]) => [
          key,
          { mode: g.mode, default: g.defaultMode, reason: g.reason, voices: g.voices.map((v) => v.id) },
        ]),
      );
      expect(got).toEqual(c.groups);
    });
  });

  it.each(TABLE.roomNames.rooms)('%s reads as a room', (name) => expect(looksLikeRoom(name)).toBe(true));
  it.each(TABLE.roomNames.people)('%s does not read as a room', (name) => expect(looksLikeRoom(name)).toBe(false));
  it.each(TABLE.roomNames.unknown)('"%s" stands for nobody', (name) => expect(isUnknownName(name)).toBe(true));
});

describe('voices waiting for a name, by the table the resolver is tested against', () => {
  NAMEABLE.cases.forEach((c) => {
    it(c.name, () => {
      expect([...nameableVoiceKeys(segmentsOf(c), identitiesOf(c))].sort()).toEqual(c.keys);
    });
  });
});

describe('storing a mode', () => {
  it('is recorded as a person choosing it, and survives a round trip', () => {
    const next = withSpeakerMode(parseSpeakerIdentities(null), 'REMOTE#ConfRm_1', ONE_PERSON, 'now');
    expect(next.speakerModes['REMOTE#ConfRm_1']).toEqual({ mode: ONE_PERSON, source: 'manual', updatedAt: 'now' });
    const reread = parseSpeakerIdentities(serializeSpeakerIdentities(next));
    expect(reread.speakerModes['REMOTE#ConfRm_1'].mode).toBe(ONE_PERSON);
  });

  it('returns a group to its default when the mode is cleared', () => {
    const chosen = withSpeakerMode(parseSpeakerIdentities(null), 'REMOTE#Bob', SEVERAL_PEOPLE);
    expect(withSpeakerMode(chosen, 'REMOTE#Bob', null).speakerModes).toEqual({});
  });

  it('keeps sections a newer version wrote', () => {
    const reread = parseSpeakerIdentities(
      serializeSpeakerIdentities(parseSpeakerIdentities('{"identities": {}, "fromANewerVersion": {"x": 1}}')),
    );
    expect(reread.fromANewerVersion).toEqual({ x: 1 });
  });

  it('says why a group is shown as it is', () => {
    const groups = speakerGroups(
      [{ channel: 'REMOTE', speaker: 'ConfRm_1 (spk_0)', startTime: 0, endTime: 1 }],
      parseSpeakerIdentities(null),
    );
    expect(describeSpeakerMode(groups['REMOTE#ConfRm_1'])).toMatch(/reads as a room/);
  });

  it("the toggle's tooltip names the mode, why, and what a click does", () => {
    const groups = speakerGroups(
      [
        { channel: 'REMOTE', speaker: 'ConfRm_1 (spk_0)', startTime: 0, endTime: 1 },
        { channel: 'REMOTE', speaker: 'Bob (spk_0)', startTime: 1, endTime: 100 },
        { channel: 'REMOTE', speaker: 'Bob (spk_1)', startTime: 100, endTime: 101 },
      ],
      parseSpeakerIdentities(null),
    );
    expect(speakerModeTip(groups['REMOTE#Bob'])).toEqual({
      mode: 'One person: every voice under "Bob" is shown as one speaker.',
      reason: describeSpeakerMode(groups['REMOTE#Bob']),
      action: 'Click if several people share this name.',
    });
    const room = speakerModeTip(groups['REMOTE#ConfRm_1']);
    expect(room.mode).toBe('Several people: each voice under "ConfRm_1" is shown on its own.');
    expect(room.action).toBe('Click to show them as one person.');
    // A viewer who may not change it is told the mode and why, and not to click.
    expect(speakerModeTip(groups['REMOTE#Bob'], false).action).toBeNull();
  });
});

describe('without groups', () => {
  it('every voice label is shown, as before speaker modes', () => {
    const segment = { channel: 'REMOTE', speaker: 'Bob (spk_1)' };
    expect(resolveSpeakerName(segment, parseSpeakerIdentities(null))).toBe('Bob (spk_1)');
  });
});
