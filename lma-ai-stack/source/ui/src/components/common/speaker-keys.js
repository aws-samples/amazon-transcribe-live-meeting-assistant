/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The keys a meeting's speaker identity map is stored under, and the merge chain.
 *
 * Shared by speaker-identities.js (names for voices) and speaker-modes.js (one
 * person or several per platform name), which both read the same map; kept here
 * so neither has to import the other.
 */
import { canonicalChannel } from './channels';
import { splitDiarizationLabel } from './utilities';

/**
 * Key for one voice: its channel, by its current name, plus the speaker string AS STORED.
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
export const speakerKey = (channel, speaker) => `${canonicalChannel(channel) ?? ''}#${(speaker ?? '').trim()}`;

/** Key for the voices under one name on one channel: `REMOTE#ConfRm_1`. */
export const groupKey = (channel, name) => `${canonicalChannel(channel) ?? ''}#${(name ?? '').trim()}`;

/** The group a voice key belongs to: `REMOTE#ConfRm_1 (spk_0)` is in `REMOTE#ConfRm_1`. */
export const groupOfKey = (key) => {
  const at = key.indexOf('#');
  const channel = at < 0 ? '' : key.slice(0, at);
  return groupKey(channel, splitDiarizationLabel(key.slice(at + 1)).base);
};

/** `map[key]` when `map` holds it as its own property, else undefined. */
export const own = (map, key) =>
  key !== undefined && Object.prototype.hasOwnProperty.call(map ?? {}, key) ? map[key] : undefined;

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
  let next = own(merges, current);
  while (typeof next === 'string' && next && !seen.has(next)) {
    current = next;
    seen.add(current);
    next = own(merges, current);
  }
  return current;
};
