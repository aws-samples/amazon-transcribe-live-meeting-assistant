/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
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
 * embeddable transcript (EmbedCallDetails), and -- in Python, see the twin,
 * lma_speakers in the configuration layer -- every LLM-facing transcript. A read
 * point that skips resolution shows voice ids while the others show names, which
 * is worse than showing voice ids everywhere.
 *
 * Each of them passes `speakerGroups` (speaker-modes.js) over the whole
 * transcript as resolveSpeaker's last argument, which is what shows one
 * person's several voice labels under one name and a room's voices apart.
 *
 * These helpers are pure and import no UI, so they stay unit-testable and usable
 * from the non-React embed path.
 */
import { canonicalChannel, canonicalKeyed, isSpeechChannel, REMOTE_CHANNEL } from './channels';
import { DEFAULT_DESKTOP_SYSTEM_LABEL, DEFAULT_LOCAL_SPEAKER_NAME, DEFAULT_OTHER_SPEAKER_NAME } from './constants';
import { own, resolveMergeTarget, speakerKey } from './speaker-keys';
import { resolveInGroup, speakerGroups } from './speaker-modes';
import { splitDiarizationLabel } from './utilities';

export { resolveMergeTarget, speakerKey, speakerGroups };

/** Current shape of the stored JSON. Bump when a migration is needed. */
export const SPEAKER_IDENTITIES_VERSION = 1;

/** An empty, well-formed map. Every reader can assume these four keys exist. */
export const emptyIdentities = () => ({
  version: SPEAKER_IDENTITIES_VERSION,
  candidates: [],
  identities: {},
  merges: {},
  segmentOverrides: {},
  // Names a user has rejected for a voice, so an automatic pass does not propose
  // the same one straight back. Keyed the same way as `identities`.
  rejected: {},
  // Per platform name (`<channel>#<name>`): whether its voices are one person or
  // several, when somebody chose (speaker-modes.js).
  speakerModes: {},
  // When the backend last ran a naming pass, and how long the transcript was.
  // Carried through parse and serialize rather than dropped: a UI write that
  // lost it would reset the backend's own cost bound on every rename.
  lastPass: undefined,
});

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
  // A map written before 0.4.0 keys its voices under the older channel names
  // (CALLER#..., AGENT#...). Re-keyed here, so the next write stores the current ones.
  // Keys this version does not know are carried through, so a write from here
  // does not erase what a newer version stored.
  const known = Object.keys(emptyIdentities());
  const unknown = Object.fromEntries(Object.entries(source).filter(([key]) => !known.includes(key)));
  return {
    ...unknown,
    version: source.version ?? SPEAKER_IDENTITIES_VERSION,
    candidates: Array.isArray(source.candidates) ? source.candidates.filter((c) => typeof c === 'string') : [],
    identities: canonicalKeyed(source.identities && typeof source.identities === 'object' ? source.identities : {}),
    merges: canonicalKeyed(source.merges && typeof source.merges === 'object' ? source.merges : {}, true),
    segmentOverrides:
      source.segmentOverrides && typeof source.segmentOverrides === 'object' ? source.segmentOverrides : {},
    rejected: canonicalKeyed(source.rejected && typeof source.rejected === 'object' ? source.rejected : {}),
    speakerModes: canonicalKeyed(
      source.speakerModes && typeof source.speakerModes === 'object' ? source.speakerModes : {},
    ),
    lastPass: source.lastPass && typeof source.lastPass === 'object' ? source.lastPass : undefined,
  };
};

export const serializeSpeakerIdentities = (identities) =>
  JSON.stringify({ ...emptyIdentities(), ...identities, version: SPEAKER_IDENTITIES_VERSION });

/**
 * The key a name for this segment must be STORED under.
 *
 * Not the same as the segment's own key once voices have been merged: the name
 * lives on the merge target, and resolution reads only the target. Writing to
 * the segment's own key instead would succeed, change nothing on screen, and
 * leave an entry the panel never shows.
 */
export const identityKeyFor = ({ channel, speaker }, identities) =>
  resolveMergeTarget(speakerKey(channel, speaker), (identities ?? emptyIdentities()).merges);

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
 *
 * `groups` is speakerGroups over the whole transcript. With it, a platform name's
 * voices are shown as one person or several (speaker-modes.js); `group` in the
 * result is the group key the segment was resolved under, if any. Without it,
 * every voice label is shown, as before speaker modes existed.
 */
export const resolveSpeaker = ({ channel, speaker, segmentId }, identities, fallbackName, groups = undefined) => {
  const map = identities ?? emptyIdentities();

  const target = resolveMergeTarget(speakerKey(channel, speaker), map.merges);
  const entry = own(map.identities, target);
  const locked = entry?.locked === true;

  const override = own(map.segmentOverrides, segmentId);
  if (override) {
    return { name: override, status: 'confirmed', overridden: true, locked };
  }

  const inGroup = resolveInGroup({ channel, speaker }, map, groups);
  if (inGroup) {
    return { ...inGroup, overridden: false };
  }

  if (entry && entry.name) {
    return { name: entry.name, status: entry.status ?? 'confirmed', overridden: false, locked };
  }

  // No mapping: today's behaviour, including the voice id, which is load-bearing.
  // Two voices on one channel differ ONLY by that suffix, so dropping it merges
  // two people into one apparent speaker.
  const { base, suffix } = splitDiarizationLabel(speaker);
  if (base === DEFAULT_OTHER_SPEAKER_NAME || base === '') {
    return {
      name: `${fallbackName || DEFAULT_OTHER_SPEAKER_NAME}${suffix}`,
      status: 'unnamed',
      overridden: false,
      locked,
    };
  }
  return { name: `${base}${suffix}`, status: 'unnamed', overridden: false, locked };
};

/** Convenience wrapper for the call sites that only want the string. */
export const resolveSpeakerName = (segment, identities, fallbackName, groups = undefined) =>
  resolveSpeaker(segment, identities, fallbackName, groups).name;

/** Whether a segment's channel carries a nameable human voice: the two speech channels. */
const isNameable = (segment) => isSpeechChannel(segment.channel);

/**
 * Stored base names that stand for "somebody not yet named". Mirrors
 * PLACEHOLDER_SPEAKERS in the speaker-name resolver, so a voice under any of
 * them -- including a whole unseparated channel -- counts as unnamed here exactly
 * when the resolver will work on it.
 */
const PLACEHOLDER_BASES = [DEFAULT_OTHER_SPEAKER_NAME, DEFAULT_LOCAL_SPEAKER_NAME, DEFAULT_DESKTOP_SYSTEM_LABEL, ''];

/**
 * Channel-wide labels the engine has split into several voices.
 *
 * The participant name on the Stream Audio form is free text, so a channel can
 * be called anything -- 'Others', or the user's address on the microphone. When
 * every segment on a channel carries ONE base name and that name appears with
 * two or more voice ids, it labels the channel rather than a person, and those
 * voices need naming like any placeholder. A Virtual Participant channel carries
 * a different roster name per attendee, so it never matches. The resolver's
 * channel_label_bases applies the same test.
 */
const channelLabelBases = (segments) => {
  const bases = {};
  const voiceIds = {};
  (segments ?? []).forEach((segment) => {
    // A segment with no words is not a voice: the resolver's turns skip it too.
    if (segment.isPartial || !isNameable(segment) || !`${segment.transcript ?? ''}`.trim()) {
      return;
    }
    const { base, suffix } = splitDiarizationLabel(segment.speaker);
    const channel = canonicalChannel(segment.channel);
    if (!bases[channel]) {
      bases[channel] = new Set();
      voiceIds[channel] = new Set();
    }
    bases[channel].add(base);
    if (suffix.trim()) {
      voiceIds[channel].add(suffix.trim());
    }
  });
  const out = new Set();
  Object.entries(bases).forEach(([channel, names]) => {
    if (names.size === 1 && voiceIds[channel].size >= 2) {
      out.add(`${channel}#${[...names][0]}`);
    }
  });
  return out;
};

/**
 * The voices still waiting for a name: what an automatic naming pass could work on.
 *
 * The same rule as unresolved_voices in the speaker-name resolver, and tested
 * against the same table (test-fixtures/nameable-voices.json). The Speakers
 * panel runs automatic passes while this is not empty, so a looser count here
 * than there means a panel asking, for as long as it is open, for passes the
 * resolver declines: a roster name with a voice label is already named, and only
 * placeholders and a channel's own label split into voices are open. A voice
 * holding an unconfirmed suggestion is still open; one a person settled (named
 * by hand, or locked) or merged into another is not.
 */
export const nameableVoiceKeys = (segments, identities) => {
  const map = identities ?? emptyIdentities();
  const spoken = (segments ?? []).filter(
    (segment) => !segment.isPartial && isNameable(segment) && `${segment.transcript ?? ''}`.trim(),
  );
  const labels = channelLabelBases(spoken);
  const keys = [];
  [...spoken]
    .sort((a, b) => (a.startTime ?? 0) - (b.startTime ?? 0))
    .forEach((segment) => {
      const key = speakerKey(segment.channel, segment.speaker);
      if (keys.includes(key) || own(map.merges, key)) return;
      const entry = own(map.identities, key);
      if (entry?.locked === true || entry?.source === 'manual') return;
      if (typeof entry?.name === 'string' && entry.name && entry.status !== 'proposed') return;
      const { base } = splitDiarizationLabel(`${segment.speaker ?? ''}`.trim());
      if (!PLACEHOLDER_BASES.includes(base) && !labels.has(`${canonicalChannel(segment.channel)}#${base}`)) return;
      keys.push(key);
    });
  return keys;
};

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
  // The participant name from the Stream Audio form is the REMOTE side's, so it
  // substitutes for the placeholder on the REMOTE channel only -- the same rule
  // every transcript reader applies. Without the channel test the panel's
  // placeholder for a local-channel voice would disagree with the transcript
  // beside it.
  const fallbackFor = (channel) => (canonicalChannel(channel) === REMOTE_CHANNEL ? fallbackName : undefined);
  const labels = channelLabelBases(segments);
  // Whether a voice is waiting for a name -- what the panel's automatic passes
  // and unnamed count act on. Distinct from `generic` below, which only decides
  // whether the Stream Audio participant name substitutes in the display.
  const needsNaming = (channel, base) =>
    PLACEHOLDER_BASES.includes(base) || labels.has(`${canonicalChannel(channel)}#${base}`);

  (segments ?? []).forEach((segment) => {
    if (segment.isPartial || !isNameable(segment)) {
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
      if (key === target && !existing.describesTarget) {
        // The row was created from a merged-away voice, because that voice was
        // seen first -- segments are grouped per channel, not ordered by time.
        // Now that the target's own segment has turned up, take the badge and
        // placeholder from it, so the row is not labelled with the voice id of
        // something that was folded into it.
        const fromTarget = splitDiarizationLabel(segment.speaker);
        const generic = fromTarget.base === DEFAULT_OTHER_SPEAKER_NAME || fromTarget.base === '';
        existing.storedSpeaker = segment.speaker;
        existing.voiceId = fromTarget.suffix.trim();
        existing.baseName = fromTarget.base;
        existing.defaultName = generic ? fallbackFor(segment.channel) || DEFAULT_OTHER_SPEAKER_NAME : fromTarget.base;
        existing.isPlaceholder = needsNaming(segment.channel, fromTarget.base);
        existing.channel = canonicalChannel(segment.channel);
        existing.describesTarget = true;
      }
      return;
    }
    const entry = own(map.identities, target);
    const { base, suffix } = splitDiarizationLabel(segment.speaker);
    rows.set(target, {
      key: target,
      channel: canonicalChannel(segment.channel),
      storedSpeaker: segment.speaker,
      voiceId: suffix.trim(),
      // The channel's own name, with the engine's voice id stripped off.
      baseName: base,
      // What the transcript shows for this voice when it has no name yet.
      defaultName:
        base === DEFAULT_OTHER_SPEAKER_NAME || base === ''
          ? fallbackFor(segment.channel) || DEFAULT_OTHER_SPEAKER_NAME
          : base,
      // True when the stored speaker is the generic placeholder rather than
      // something that already reads as a person (a meeting-roster name, or an
      // address the client supplied). Drives whether the panel has work to do.
      isPlaceholder: needsNaming(segment.channel, base),
      name: entry?.name ?? '',
      status: entry?.name ? entry.status ?? 'confirmed' : 'unnamed',
      source: entry?.source,
      evidence: entry?.evidence,
      locked: entry?.locked === true,
      turns: 1,
      talkTimeSeconds: duration,
      firstHeardAt: segment.startTime ?? 0,
      mergedKeys: key !== target ? [key] : [],
      // False when the row was seeded from a voice that is merged INTO this one,
      // meaning its display fields are still provisional.
      describesTarget: key === target,
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

/**
 * Did separation actually split a channel into more than one voice?
 *
 * Drives whether the Speakers panel starts open. Every meeting has speakers, so a
 * panel that is always expanded pushes the transcript down the page for the
 * majority of meetings that have one voice per channel and nothing to
 * disambiguate. It starts open only where it has work to do.
 */
export const hasMultipleVoicesOnAChannel = (roster) => {
  // The diagnostic signal is two UNNAMED voices that share a channel AND a base
  // name, differing only by the engine's voice id -- that is one audio source
  // the engine has split into several people, which is exactly what the panel
  // disambiguates.
  //
  // Neither looser rule works. Counting rows per channel opens the panel on a
  // Virtual Participant meeting, where each attendee is already a row under
  // their meeting-roster name. Counting rows that carry a voice id does the
  // same, because roster names carry one too. Requiring the generic placeholder
  // goes too far the other way: a client that supplies its own channel name --
  // an address, say -- produces 'alex@example.com (spk_0)' and '(spk_1)', which
  // is two real people behind one tab and does need naming.
  const groups = {};
  (roster ?? [])
    .filter((row) => !row.name && row.voiceId)
    .forEach((row) => {
      const group = `${row.channel}#${row.baseName}`;
      groups[group] = (groups[group] || 0) + 1;
    });
  return Object.values(groups).some((count) => count > 1);
};

/** How a roster row is labelled: its name, or the placeholder with the voice id that tells it apart. */
export const speakerRowLabel = (row) => row.name || `${row.defaultName} ${row.voiceId || ''}`.trim();

/**
 * The roster's rows as the transcript shows them.
 *
 * Each row gains `shownAs` (the name its lines carry, through the meeting's
 * speaker modes -- 'Bob Strahan' for every voice of a name shown as one person,
 * 'ConfRm_1 · Voice 2' for an unnamed voice of a name shown as several),
 * `shownUnnamed` (that name is a placeholder nobody gave), and `group` (the
 * key of the platform name it was resolved under, if any). `groups` is
 * speakerGroups over the same segments the roster was built from.
 */
export const speakerDisplayRows = (roster, identities, groups, fallbackName) =>
  (roster ?? []).map((row) => {
    const resolved = resolveSpeaker(
      { channel: row.channel, speaker: row.storedSpeaker },
      identities,
      canonicalChannel(row.channel) === REMOTE_CHANNEL ? fallbackName : undefined,
      groups,
    );
    return { ...row, shownAs: resolved.name, shownUnnamed: resolved.status === 'unnamed', group: resolved.group };
  });

/**
 * The speakers named in the meeting page's header: one per name the transcript
 * shows, in the order first heard, so the header lists exactly the speakers the
 * transcript's lines carry. Takes speakerDisplayRows' rows; each entry is
 * `{ key, label, keys }`, `key` being the first voice under the name (the row
 * the Speakers panel opens at) and `keys` every voice under it. Rows are merged
 * by the label alone, deliberately: two different voices given the same name
 * read as one speaker in the transcript, so they are one entry here too.
 */
export const headerSpeakers = (displayRows) => {
  const byLabel = new Map();
  (displayRows ?? []).forEach((row) => {
    const label = `${row.shownAs ?? ''}`.trim() || speakerRowLabel(row);
    const entry = byLabel.get(label);
    if (entry) {
      entry.keys.push(row.key);
      return;
    }
    byLabel.set(label, { key: row.key, label, keys: [row.key] });
  });
  return [...byLabel.values()];
};

/**
 * The map with the voice `key` given `name` by a person: confirmed, and
 * recorded as manual so no automatic pass revisits it. An empty name removes
 * the voice's entry, so the transcript falls back to the voice id rather than
 * showing an empty speaker.
 */
export const withSpeakerName = (identities, key, name, now = new Date().toISOString()) => {
  const next = { ...identities.identities };
  if (name) {
    next[key] = { ...(next[key] || {}), name, status: 'confirmed', source: 'manual', updatedAt: now };
  } else {
    delete next[key];
  }
  return { ...identities, identities: next };
};

/** The map with the voice `key` folded into `targetKey`: one person, shown under the target's name. */
export const withMerge = (identities, key, targetKey) => ({
  ...identities,
  merges: { ...identities.merges, [key]: targetKey },
});

/** The map with the voices `keys` separated again from whatever they were merged into. */
export const withoutMerges = (identities, keys) => {
  const merges = { ...identities.merges };
  (keys ?? []).forEach((key) => delete merges[key]);
  return { ...identities, merges };
};

/** mm:ss for a talk-time total. */
export const formatTalkTime = (seconds) => {
  const total = Math.round(seconds ?? 0);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${String(secs).padStart(2, '0')}`;
};
