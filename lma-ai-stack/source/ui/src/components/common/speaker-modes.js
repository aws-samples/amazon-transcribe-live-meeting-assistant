/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * One person or several: how the voices under one platform name are shown.
 *
 * On a diarized meeting a segment's stored speaker is the name the meeting
 * platform showed plus the engine's voice label -- 'Bob Strahan (spk_0)'. For an
 * individual the label is noise: one person's voice is routinely split into two
 * or three labels, and the transcript should just say 'Bob Strahan'. For a
 * conference room ('ConfRm_1') the labels are the only thing telling the people
 * in it apart, and each needs to be nameable on its own.
 *
 * So each name that carries voice labels is a GROUP, keyed `<channel>#<name>`,
 * shown in one of two modes:
 *
 *  - ONE PERSON: every line shows the name alone (or the one name its voices
 *    were given);
 *  - SEVERAL PEOPLE: each voice is shown on its own as 'ConfRm_1 · Voice 1',
 *    'Voice 2'... until it is named, numbered in the order they were first heard.
 *
 * A user's choice is stored in the meeting's speaker identity map, under
 * `speakerModes`; without one the mode is computed from the transcript: several
 * people when the name reads as a room or a room device, or when its voices were
 * given different names; one person otherwise. How many voices a name has, and
 * how long each spoke, do not decide it: speaker separation routinely splits one
 * person into two or three voices that each speak at length. The rules are in
 * speaker-mode-rules.json, a byte-identical copy of the Python layer's.
 *
 * Names that stand for nobody in particular -- 'Unknown', the 'Other
 * Participant' placeholder -- form no group and keep their voice labels.
 *
 * The Python twin is lma_speakers in the configuration layer, which every
 * LLM-facing transcript goes through; both are tested against
 * test-fixtures/speaker-modes.json.
 */
import RULES from './speaker-mode-rules.json';
import { canonicalChannel, isSpeechChannel } from './channels';
import { groupKey, groupOfKey, own, resolveMergeTarget, speakerKey } from './speaker-keys';
import { splitDiarizationLabel } from './utilities';

export const ONE_PERSON = 'one';
export const SEVERAL_PEOPLE = 'several';
const MODES = [ONE_PERSON, SEVERAL_PEOPLE];

/** Why a group has the mode it has when nobody chose one. */
export const REASON_ROOM_NAME = 'room-name';
export const REASON_NAMED_VOICES = 'named-voices';
export const REASON_DEFAULT = 'one-person';

const UNKNOWN_NAMES = new Set(RULES.unknownNames.map((name) => name.toLowerCase()));
const ROOM_NAME_PATTERNS = RULES.roomNamePatterns.map(({ pattern }) => new RegExp(pattern, 'i'));

/** How an unnamed voice of a name shown as several people reads. */
export const voiceLabel = (name, ordinal) => `${name} · Voice ${ordinal}`;

/** A name that stands for nobody in particular, whose voice labels are kept. */
export const isUnknownName = (name) => UNKNOWN_NAMES.has(`${name ?? ''}`.trim().toLowerCase());

/** Whether a platform name reads as a room or a room device rather than a person. */
export const looksLikeRoom = (name) => {
  const value = `${name ?? ''}`.trim();
  return Boolean(value) && ROOM_NAME_PATTERNS.some((pattern) => pattern.test(value));
};

const seconds = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
};

const voiceNumber = (id) => Number(id.split('_')[1]);

/**
 * The meeting's names that carry voice labels, each with its voices and mode.
 *
 * Built from the final speech segments of the WHOLE transcript: a name's voices,
 * their Voice 1, 2... order and the names they were given are properties of the
 * meeting. Returns an object keyed by
 * group key; each group has `base` (the platform name), `channel`, `voices`
 * ({ id, key, seconds, first, ordinal } in Voice 1, 2... order), `members` (the
 * stored speaker keys), `defaultMode` and `reason`, `override` (the stored
 * choice, if any), `mode`, and `name` / `nameStatus` / `nameLocked` (what the
 * group goes by as one person).
 */
export const speakerGroups = (segments, identities) => {
  const groups = {};
  (segments ?? []).forEach((segment) => {
    if (segment.isPartial === true) return;
    const channel = canonicalChannel(segment.channel);
    if (!isSpeechChannel(channel)) return;
    const stored = `${segment.speaker ?? ''}`.trim();
    const split = splitDiarizationLabel(stored);
    const base = split.base.trim();
    if (isUnknownName(base)) return;
    const key = groupKey(channel, base);
    const start = seconds(segment.startTime);
    const duration = Math.max(0, seconds(segment.endTime) - start);
    if (!groups[key]) {
      groups[key] = { key, channel, base, seconds: 0, voices: {}, members: new Set() };
    }
    const group = groups[key];
    group.seconds += duration;
    group.members.add(speakerKey(channel, stored));
    if (split.suffix) {
      const id = split.suffix.trim().slice(1, -1);
      if (!group.voices[id]) group.voices[id] = { id, key: speakerKey(channel, stored), seconds: 0, first: start };
      const voice = group.voices[id];
      voice.seconds += duration;
      voice.first = Math.min(voice.first, start);
    }
  });

  const names = identities?.identities ?? {};
  const merges = identities?.merges ?? {};
  const modes = identities?.speakerModes ?? {};
  Object.values(groups).forEach((group) => {
    const g = group;
    g.voices = Object.values(g.voices).sort((a, b) => a.first - b.first || voiceNumber(a.id) - voiceNumber(b.id));
    g.voices.forEach((voice, index) => {
      // eslint-disable-next-line no-param-reassign
      voice.ordinal = index + 1;
    });

    // The names given to this group's voices, through any merge that stays
    // within the group. A voice merged into someone else resolves as that
    // person and takes no part here.
    const given = new Map();
    [...g.members].sort().forEach((member) => {
      const target = resolveMergeTarget(member, merges);
      if (groupOfKey(target) !== g.key) return;
      const entry = own(names, target);
      const name = typeof entry?.name === 'string' ? entry.name.trim() : '';
      if (name && !given.has(name)) given.set(name, entry);
    });

    // Neither the number of voices nor their share of the speech is a rule:
    // both are what a diarizer splitting one person produces.
    if (looksLikeRoom(g.base)) {
      g.defaultMode = SEVERAL_PEOPLE;
      g.reason = REASON_ROOM_NAME;
    } else if (given.size >= 2) {
      g.defaultMode = SEVERAL_PEOPLE;
      g.reason = REASON_NAMED_VOICES;
    } else {
      g.defaultMode = ONE_PERSON;
      g.reason = REASON_DEFAULT;
    }
    const chosen = own(modes, g.key);
    const override = chosen && typeof chosen === 'object' ? chosen.mode : undefined;
    g.override = MODES.includes(override) ? override : null;
    g.mode = g.override ?? g.defaultMode;

    if (given.size === 1) {
      const [[name, entry]] = [...given.entries()];
      g.name = name;
      g.nameStatus = entry.status || 'confirmed';
      g.nameLocked = entry.locked === true;
    } else {
      g.name = g.base;
      g.nameStatus = null;
      g.nameLocked = false;
    }
    g.members = [...g.members];
  });
  return groups;
};

/**
 * How one segment's speaker reads under its group, or null when no group applies.
 *
 * Called by resolveSpeaker after a name given to the segment alone, and before
 * the voice's own name: a group shown as one person goes by one name whatever
 * its voices were called, and a voice of a group shown as several people goes by
 * its own name, else 'Name · Voice N'. A voice merged into someone outside the
 * group is that person, so no group applies to it.
 */
export const resolveInGroup = ({ channel, speaker }, identities, groups) => {
  if (!groups) return null;
  const stored = `${speaker ?? ''}`.trim();
  const { base, suffix } = splitDiarizationLabel(stored);
  const key = groupKey(channel, base.trim());
  const group = own(groups, key);
  if (!group) return null;
  const target = resolveMergeTarget(speakerKey(channel, stored), identities?.merges ?? {});
  if (groupOfKey(target) !== key) return null;
  if (group.mode === ONE_PERSON) {
    return { name: group.name, status: group.nameStatus || 'unnamed', locked: group.nameLocked, group: key };
  }
  const entry = own(identities?.identities ?? {}, target);
  if (entry?.name)
    return { name: entry.name, status: entry.status ?? 'confirmed', locked: entry.locked === true, group: key };
  if (!suffix) return null;
  const targetId = splitDiarizationLabel(target.slice(target.indexOf('#') + 1))
    .suffix.trim()
    .slice(1, -1);
  const voice = group.voices.find((v) => v.id === targetId);
  return voice ? { name: voiceLabel(group.base, voice.ordinal), status: 'unnamed', locked: false, group: key } : null;
};

/** The map with the voices under the group `key` shown as `mode`; a null mode returns the group to its default. */
export const withSpeakerMode = (identities, key, mode, now = new Date().toISOString()) => {
  const speakerModes = { ...(identities.speakerModes || {}) };
  if (MODES.includes(mode)) {
    speakerModes[key] = { mode, source: 'manual', updatedAt: now };
  } else {
    delete speakerModes[key];
  }
  return { ...identities, speakerModes };
};

/**
 * Whether a group's voices can be shown either way, so it is offered the
 * One person / Several people choice: it has two or more voices, or it is shown
 * as several people. The chips above the transcript and the Speakers panel
 * apply the same test.
 */
export const offersModeChoice = (group) =>
  Boolean(group) && (group.voices.length >= 2 || group.mode === SEVERAL_PEOPLE);

/**
 * The Transcript tab's speaker chips, grouped by platform name.
 *
 * `segments` are the transcript's speech lines as resolved for display, in
 * order: each with `speaker` (the name shown), `speakerKey` (the voice key a
 * name is stored under) and `speakerGroup` (the group key resolveSpeaker
 * returned). Returns, in order of first appearance:
 *
 *  - `{ type: 'group', key, base, mode, group, chips }` for a name whose voices
 *    can be shown either way -- one with two or more voices, or one shown as
 *    several people -- with one chip per name shown under it (`{ name, keys,
 *    unnamed }`, `unnamed` when the name is the 'Voice N' placeholder rather
 *    than one somebody gave, from the segment's `speakerUnnamed`);
 *  - `{ type: 'name', name, keys }` for every other speaker.
 */
export const speakerChipLayout = (segments, groups) => {
  const entries = [];
  const byGroup = new Map();
  const byName = new Map();
  const addKey = (target, key) => {
    if (key && !target.keys.includes(key)) target.keys.push(key);
  };
  (segments ?? []).forEach((segment) => {
    const name = `${segment.speaker ?? ''}`.trim();
    const group = segment.speakerGroup ? own(groups, segment.speakerGroup) : undefined;
    if (offersModeChoice(group)) {
      let entry = byGroup.get(group.key);
      if (!entry) {
        entry = { type: 'group', key: group.key, base: group.base, mode: group.mode, group, chips: [] };
        byGroup.set(group.key, entry);
        entries.push(entry);
      }
      let chip = entry.chips.find((c) => c.name === name);
      if (!chip) {
        chip = { name, keys: [], unnamed: segment.speakerUnnamed === true };
        entry.chips.push(chip);
      }
      addKey(chip, segment.speakerKey);
      return;
    }
    let entry = byName.get(name);
    if (!entry) {
      entry = { type: 'name', name, keys: [] };
      byName.set(name, entry);
      entries.push(entry);
    }
    addKey(entry, segment.speakerKey);
  });
  return entries;
};

/** Why a group is shown as it is, for the one-person / several-people toggle's tooltip. */
export const describeSpeakerMode = (group) => {
  if (!group) return '';
  if (group.override) return 'Chosen for this meeting.';
  switch (group.reason) {
    case REASON_ROOM_NAME:
      return `"${group.base}" reads as a room, so its voices are shown separately.`;
    case REASON_NAMED_VOICES:
      return 'Its voices were given different names, so they are shown separately.';
    default:
      return 'Shown as one person unless the name reads as a room: speaker separation often splits one voice in two.';
  }
};

/**
 * What the one-person / several-people toggle's tooltip says for a group: the
 * mode it is shown in, why (describeSpeakerMode), and -- when the viewer may
 * change it -- what a click does. `{ mode, reason, action }`, with action null
 * for a viewer who may not change it.
 */
export const speakerModeTip = (group, canChange = true) => {
  if (!group) return { mode: '', reason: '', action: null };
  const several = group.mode === SEVERAL_PEOPLE;
  let action = null;
  if (canChange) action = several ? 'Click to show them as one person.' : 'Click if several people share this name.';
  return {
    mode: several
      ? `Several people: each voice under "${group.base}" is shown on its own.`
      : `One person: every voice under "${group.base}" is shown as one speaker.`,
    reason: describeSpeakerMode(group),
    action,
  };
};
