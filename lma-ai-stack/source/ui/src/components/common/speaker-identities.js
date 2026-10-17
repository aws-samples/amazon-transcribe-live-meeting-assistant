/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Per-meeting map from a transcript segment's stored speaker to a real name.
 *
 * Why a map rather than rewriting the segments:
 *
 *  A segment's `Speaker` is written once, by the transcriber, and a diarized
 *  meeting stores a voice id rather than a name -- 'Other Participant (spk_0)'.
 *  Naming that voice has to apply to every segment it already produced, and
 *  rewriting them would mean one addTranscriptSegment mutation per segment, each
 *  of which fans out through the onAddTranscriptSegment subscription to every
 *  connected client. Segments also carry their own, earlier TTL than the meeting,
 *  so a late rename can race expiry.
 *
 *  So the stored speaker stays as the transcriber wrote it -- it is the only
 *  record of which voice the engine actually heard -- and the name is applied
 *  wherever the speaker is READ. A rename is then one small write, is retroactive
 *  across the whole transcript by construction, and is reversible.
 *
 * Four places read a speaker and so must resolve through here: the live
 * transcript (CallPanel), the transcript download (download-func), the
 * embeddable transcript (EmbedCallDetails), and -- in Python, see the twin in
 * lambda_functions/fetch_transcript -- every LLM-facing transcript. A read point
 * that skips resolution shows voice ids while the others show names, which is
 * worse than showing voice ids everywhere.
 *
 * These helpers are pure and import no UI, so they stay unit-testable and usable
 * from the non-React embed path.
 */
import { DEFAULT_OTHER_SPEAKER_NAME } from './constants';
import { splitDiarizationLabel } from './utilities';

/** Current shape of the stored JSON. Bump when a migration is needed. */
export const SPEAKER_IDENTITIES_VERSION = 1;

/** An empty, well-formed map. Every reader can assume these four keys exist. */
export const emptyIdentities = () => ({
  version: SPEAKER_IDENTITIES_VERSION,
  candidates: [],
  identities: {},
  merges: {},
  segmentOverrides: {},
});

/**
 * Key for one voice: its channel plus the speaker string AS STORED.
 *
 * Keying on the channel and voice id alone would collide: ids are numbered per
 * channel and restart at spk_0, so on a Virtual Participant meeting -- where the
 * base name comes from the meeting roster -- 'Alice (spk_0)' and 'Bob (spk_0)'
 * are two people sharing one key. Including the stored base name keeps them
 * apart.
 *
 * A '#' inside a name does not need escaping: the key is only ever built and
 * looked up by this function, and nothing parses a name back out of it.
 */
export const speakerKey = (channel, speaker) => `${channel ?? ''}#${(speaker ?? '').trim()}`;

/**
 * Parse the stored JSON, tolerating anything.
 *
 * A meeting that predates this feature has no attribute at all, and a blob
 * written by a future version may carry keys we do not know. Both must render a
 * transcript rather than break it, so every failure yields the empty map.
 */
export const parseSpeakerIdentities = (raw) => {
  if (!raw) {
    return emptyIdentities();
  }
  const source =
    typeof raw === 'string'
      ? (() => {
          try {
            return JSON.parse(raw);
          } catch (e) {
            return null;
          }
        })()
      : raw;
  if (!source || typeof source !== 'object') {
    return emptyIdentities();
  }
  return {
    version: source.version ?? SPEAKER_IDENTITIES_VERSION,
    candidates: Array.isArray(source.candidates) ? source.candidates.filter((c) => typeof c === 'string') : [],
    identities: source.identities && typeof source.identities === 'object' ? source.identities : {},
    merges: source.merges && typeof source.merges === 'object' ? source.merges : {},
    segmentOverrides:
      source.segmentOverrides && typeof source.segmentOverrides === 'object' ? source.segmentOverrides : {},
  };
};

export const serializeSpeakerIdentities = (identities) =>
  JSON.stringify({ ...emptyIdentities(), ...identities, version: SPEAKER_IDENTITIES_VERSION });

/**
 * Follow the merge chain to the key that actually holds the name.
 *
 * Merges exist because splitting one person across two voice ids is the common
 * diarization failure, and naming both 'Bob' without merging leaves two Bob rows
 * that then confuse every summary. The chain is followed with a visit set rather
 * than one hop: merging A into B and later B into C is a legitimate sequence, and
 * a cycle (however it got written) must terminate rather than hang the render.
 */
export const resolveMergeTarget = (key, merges) => {
  let current = key;
  const seen = new Set([current]);
  while (merges[current] && !seen.has(merges[current])) {
    current = merges[current];
    seen.add(current);
  }
  return current;
};

/**
 * Display name for one segment, and how much to trust it.
 *
 * Returns `status` alongside the name so a caller can distinguish a name a human
 * confirmed from one that was inferred. Phase 1 only ever writes 'confirmed',
 * but every call site passing the status through means an inferred name can be
 * rendered differently later without touching them.
 *
 * `fallbackName` is the participant name the user typed on the Stream Audio form.
 * Preserving that substitution here is what lets this replace the ad-hoc version
 * the transcript used to do inline: a stored 'Other Participant (spk_0)' with no
 * mapping still renders as '<their name> (spk_0)', label intact.
 */
export const resolveSpeaker = ({ channel, speaker, segmentId }, identities, fallbackName) => {
  const map = identities ?? emptyIdentities();

  const override = segmentId ? map.segmentOverrides[segmentId] : undefined;
  if (override) {
    return { name: override, status: 'confirmed', overridden: true };
  }

  const target = resolveMergeTarget(speakerKey(channel, speaker), map.merges);
  const entry = map.identities[target];
  if (entry && entry.name) {
    return { name: entry.name, status: entry.status ?? 'confirmed', overridden: false };
  }

  // No mapping: today's behaviour, including the voice id, which is load-bearing.
  // Two voices on one channel differ ONLY by that suffix, so dropping it merges
  // two people into one apparent speaker.
  const { base, suffix } = splitDiarizationLabel(speaker);
  if (base === DEFAULT_OTHER_SPEAKER_NAME || base === '') {
    return { name: `${fallbackName || DEFAULT_OTHER_SPEAKER_NAME}${suffix}`, status: 'unnamed', overridden: false };
  }
  return { name: `${base}${suffix}`, status: 'unnamed', overridden: false };
};

/** Convenience wrapper for the call sites that only want the string. */
export const resolveSpeakerName = (segment, identities, fallbackName) =>
  resolveSpeaker(segment, identities, fallbackName).name;

/** Channels that carry a nameable human voice. */
const NAMEABLE_CHANNELS = ['CALLER', 'AGENT'];

/**
 * The distinct voices in a meeting, for the Speakers panel.
 *
 * Built from the loaded segments rather than from the identity map, because the
 * map starts empty and the panel has to list the voices waiting to be named.
 * Merged-away voices are folded into their target so the panel shows one row per
 * person, not one per voice id.
 *
 * Partials are skipped: a partial has no voice id yet (labels arrive only on
 * finals), so counting them would invent a nameless row that disappears.
 */
export const buildSpeakerRoster = (segments, identities, fallbackName) => {
  const map = identities ?? emptyIdentities();
  const rows = new Map();

  (segments ?? []).forEach((segment) => {
    if (segment.isPartial || !NAMEABLE_CHANNELS.includes(segment.channel)) {
      return;
    }
    const key = speakerKey(segment.channel, segment.speaker);
    const target = resolveMergeTarget(key, map.merges);
    const existing = rows.get(target);
    const duration = Math.max(0, (segment.endTime ?? 0) - (segment.startTime ?? 0));
    if (existing) {
      existing.turns += 1;
      existing.talkTimeSeconds += duration;
      existing.firstHeardAt = Math.min(existing.firstHeardAt, segment.startTime ?? 0);
      if (key !== target && !existing.mergedKeys.includes(key)) {
        existing.mergedKeys.push(key);
      }
      return;
    }
    const entry = map.identities[target];
    const { base, suffix } = splitDiarizationLabel(segment.speaker);
    rows.set(target, {
      key: target,
      channel: segment.channel,
      storedSpeaker: segment.speaker,
      voiceId: suffix.trim(),
      // What the transcript shows for this voice when it has no name yet.
      defaultName:
        base === DEFAULT_OTHER_SPEAKER_NAME || base === '' ? fallbackName || DEFAULT_OTHER_SPEAKER_NAME : base,
      name: entry?.name ?? '',
      status: entry?.name ? entry.status ?? 'confirmed' : 'unnamed',
      source: entry?.source,
      evidence: entry?.evidence,
      locked: entry?.locked === true,
      turns: 1,
      talkTimeSeconds: duration,
      firstHeardAt: segment.startTime ?? 0,
      mergedKeys: key !== target ? [key] : [],
    });
  });

  return [...rows.values()].sort((a, b) => a.firstHeardAt - b.firstHeardAt);
};

/**
 * Pull candidate names out of whatever the user pasted.
 *
 * There is no automatic roster for a Stream Audio meeting -- the invitation
 * parser extracts only the meeting URL -- so the names, when they are available
 * at all, come from a human pasting an invite's attendee block, an email
 * recipient line or a bare list. Accept all three shapes and anything between
 * them; a meeting with nothing pasted is the normal case and must keep working.
 */
export const parseAttendeeList = (text) => {
  let remainder = text ?? '';
  const extracted = [];

  // Pull out '"Lee, Sarah" <sarah@example.com>' and 'Bob Strahan <bob@…>' FIRST.
  // A display name may legitimately contain the comma that otherwise separates
  // attendees, so splitting before extracting would cut 'Lee, Sarah' in half.
  // The display name is also the better label than the address beside it.
  [/"([^"]+)"\s*<[^>]*>/g, /([^,;\n<>"]+?)\s*<[^>]*>/g].forEach((pattern) => {
    remainder = remainder.replace(pattern, (match, display) => {
      if (display && display.trim()) {
        extracted.push(display.trim());
        return '\n';
      }
      return match;
    });
  });

  const names = [...extracted, ...remainder.split(/[\n,;]+/)]
    // Newlines, commas and semicolons separate attendees in every format people
    // actually paste, once bracketed entries are out of the way.
    .map((raw) => {
      let value = raw.trim();
      if (/^<?[^\s<>@]+@[^\s<>@]+>?$/.test(value)) {
        // A bare address: derive something human from the local part, since
        // 'bob.strahan' reads far better than the whole address in a transcript.
        const local = value.replace(/^</, '').replace(/>$/, '').split('@')[0];
        value = local
          .split(/[._-]+/)
          .filter(Boolean)
          .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
          .join(' ');
      }
      // Calendar decorations that are not part of anyone's name.
      value = value
        .replace(/\((organizer|optional|required|host|presenter|he\/him|she\/her|they\/them)\)/gi, '')
        .replace(/^"(.*)"$/, '$1')
        .replace(/\s+/g, ' ')
        .trim();
      return value;
    })
    .filter((value) => value.length > 1 && value.length <= 80);

  // Case-insensitive de-duplication, first spelling wins.
  const seen = new Set();
  return names.filter((name) => {
    const folded = name.toLowerCase();
    if (seen.has(folded)) {
      return false;
    }
    seen.add(folded);
    return true;
  });
};

/** mm:ss for a talk-time total. */
export const formatTalkTime = (seconds) => {
  const total = Math.round(seconds ?? 0);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${String(secs).padStart(2, '0')}`;
};
