/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The decisions behind audio capture and meeting identification, with no React,
 * no Chrome APIs and no network.
 *
 * These were closures inside `IntegrationProvider`, which made them unreachable
 * from a test: exercising them meant rendering a component that opens a
 * WebSocket and calls `chrome.tabs`. They are plain functions here and the
 * provider imports them, so the behaviour is unchanged but can be asserted
 * directly.
 */

/** Bytes per frame of the captured stream: 16-bit samples, two channels. */
const BYTES_PER_FRAME = 4;

/** Byte offset of the microphone channel within each frame (channel 1). */
const MIC_CHANNEL_OFFSET = 2;

/**
 * Apply the user's mute and pause choices to one chunk of captured audio.
 *
 * Pause silences everything; mute silences only the microphone channel, leaving
 * the meeting's own audio flowing. This is the code that decides whether a user
 * who pressed mute is actually silent, so the distinction matters more than the
 * mechanics: getting the channel offset or the stride wrong sends live
 * microphone audio from someone who believes they are muted.
 *
 * Pause returns a fresh array; mute modifies `dataArray` in place and returns
 * it, which is what the original did and what callers rely on.
 */
export function applyMuteAndPause(
  dataArray: Uint8Array,
  isMuted: boolean,
  isPaused: boolean,
): Uint8Array {
  if (isPaused) {
    // Every channel silent: a same-length run of zeroes keeps the stream's
    // timing intact, which dropping the chunk entirely would not.
    return new Uint8Array(dataArray.length);
  }
  if (isMuted) {
    for (let i = MIC_CHANNEL_OFFSET; i < dataArray.length; i += BYTES_PER_FRAME) {
      dataArray[i] = 0;
      dataArray[i + 1] = 0;
    }
  }
  return dataArray;
}

/** What the UI shows when the tab is not a recognised meeting platform. */
export const UNKNOWN_PLATFORM = 'n/a';

/**
 * What a meeting reader reports about the tab it is running in.
 *
 * Loosely typed on purpose: the Zoom reader forwards fields out of Zoom's own
 * `MeetingConfig` object, so the shape is not ours to fix.
 */
export type MeetingMetadata = {
  [field: string]: unknown;
};

/**
 * Fold a metadata update into what is already known.
 *
 * Readers report whatever they have found so far rather than waiting for every
 * field, and with `all_frames` enabled several frames of the same tab each
 * report independently — so an update carrying only `baseUrl` can arrive after
 * one that carried the name and topic. Overlaying only the non-blank fields
 * keeps the later, thinner update from discarding what the earlier one found.
 */
export function mergeMetadata(
  previous: MeetingMetadata | undefined | null,
  incoming: MeetingMetadata | undefined | null,
): MeetingMetadata {
  const merged: MeetingMetadata = { ...(previous || {}) };
  const update = incoming || {};
  Object.keys(update).forEach((field) => {
    const value = update[field];
    const isBlank = value === undefined
      || value === null
      || (typeof value === 'string' && value.trim() === '');
    if (!isBlank) {
      merged[field] = value;
    }
  });
  return merged;
}

/** Which page a reader's report came from: its tab, and that tab's address. */
export type ReportSource = {
  tabId?: number;
  url?: string;
};

/** What is known about a meeting, and which page it was read from. */
export type MetadataForTab = {
  source: ReportSource;
  metadata: MeetingMetadata;
};

/**
 * Is a report from the same meeting page as what is already known?
 *
 * The tab id alone is not enough. A tab keeps its id across navigation, so one
 * tab hosts a succession of meetings and the address is what changes between
 * them. Both are compared, with the query string and fragment dropped, since
 * both platforms rewrite those within a single meeting and a spurious reset
 * would throw away fields the panel has.
 *
 * That limit is worth stating: a platform that keeps the meeting id in the
 * fragment — Teams, at `/v2/#/meet/<id>` — is not distinguished here, so two
 * Teams meetings in one tab look like one page. What covers that is the Teams
 * reader re-reading the page title on every report rather than caching the
 * first one it saw.
 *
 * A report that carries neither tab nor address is treated as belonging to the
 * page in hand: there is nothing better to assume, and the alternative discards
 * what the panel has already shown the user.
 */
function isSamePage(known: ReportSource, incoming: ReportSource): boolean {
  if (incoming.tabId === undefined && incoming.url === undefined) {
    return true;
  }
  if (incoming.tabId !== undefined && known.tabId !== undefined
    && incoming.tabId !== known.tabId) {
    return false;
  }
  const pagePath = (url: string | undefined): string => {
    if (!url) {
      return '';
    }
    try {
      const parsed = new URL(url);
      return `${parsed.origin}${parsed.pathname}`;
    } catch {
      return url;
    }
  };
  if (incoming.url !== undefined && known.url !== undefined) {
    return pagePath(incoming.url) === pagePath(known.url);
  }
  return true;
}

/**
 * Fold a reader's update into what is known, scoped to the page it came from.
 *
 * Merging is only right within one meeting. Carried across meetings it would
 * bring a previous one's topic into the next, and the topic becomes the
 * meeting's name in LMA — so a meeting whose own reader found no topic would be
 * recorded under the name of the one before it. A report from a different page
 * therefore starts from nothing instead of from what the last one said.
 */
export function mergeMetadataForTab(
  known: MetadataForTab,
  incoming: MeetingMetadata | undefined | null,
  source: ReportSource = {},
): MetadataForTab {
  const samePage = isSamePage(known.source, source);
  return {
    source: {
      tabId: source.tabId !== undefined ? source.tabId : known.source.tabId,
      url: source.url !== undefined ? source.url : known.source.url,
    },
    metadata: mergeMetadata(samePage ? known.metadata : {}, incoming),
  };
}

/**
 * What a prefilled form field should hold once a reader reports a value for it.
 *
 * The readers report repeatedly — on page load, on each request from the panel,
 * and again when a late-arriving source such as Zoom's `MeetingConfig` turns up
 * minutes later — so "fill the field from the latest report" would overwrite
 * whatever the user had typed in the meantime, mid-word.
 *
 * The test is whether the field still holds exactly what we last put in it,
 * which is a single comparison that covers every case. An untouched field holds
 * our prefill, so a later report refreshes it — that is what makes switching the
 * panel to a different meeting tab update the field. A field holding anything
 * else is the user's and is left alone, *including* a field they have emptied:
 * backspacing to nothing is how someone starts retyping a topic, and refilling
 * it under them would make the field impossible to clear.
 *
 * Both the field and the record of what we prefilled start empty, which is what
 * lets the first report fill it.
 */
export function prefillField(current: string, lastPrefilled: string, reported: unknown): string {
  if (typeof reported !== 'string' || reported.trim() === '') {
    return current;
  }
  return current === lastPrefilled ? reported.trim() : current;
}

/** The hostnames one meeting platform serves its web client from. */
export type PlatformHosts = {
  platform: string;
  /** Hosts that must equal the whole hostname. */
  exact?: string[];
  /** Hosts that match themselves or any subdomain of themselves. */
  suffix?: string[];
};

/**
 * Which hostnames belong to which meeting platform.
 *
 * Exported because `public/manifest.json` has to register a content script for
 * these same hosts, and the two drifting apart is what makes the panel show no
 * platform for a meeting it is nonetheless capturing. `manifest.test.ts` checks
 * one against the other.
 *
 * Teams is the reason the suffix/exact distinction exists. Microsoft serves it
 * from `teams.microsoft.com`, from `teams.microsoft.us` for government tenants,
 * from `teams.live.com` for consumer accounts, and from `teams.cloud.microsoft`
 * since the Microsoft 365 unified-domain consolidation; a tenant may land on
 * either of the first and the last, so both have to be listed. Webex is
 * per-organisation (`acme.webex.com`), so it needs the same subdomain treatment.
 *
 * Google Meet belongs here even though it is not a Virtual Participant
 * platform: the extension is how Meet meetings are captured.
 */
export const MEETING_PLATFORM_HOSTS: PlatformHosts[] = [
  { platform: 'Zoom', suffix: ['zoom.us'] },
  { platform: 'Amazon Chime', exact: ['app.chime.aws'] },
  {
    platform: 'Microsoft Teams',
    suffix: ['teams.microsoft.com', 'teams.cloud.microsoft', 'teams.microsoft.us', 'teams.live.com'],
  },
  { platform: 'Cisco Webex', suffix: ['webex.com'] },
  { platform: 'Google Meet', exact: ['meet.google.com'] },
];

/**
 * The hostname of a reader-supplied base URL, or '' if there isn't one.
 *
 * Every reader but Zoom reports `window.location.origin`, which always carries a
 * scheme. Zoom's value comes out of Zoom's own `MeetingConfig` object, so a bare
 * hostname has to parse too — hence the second attempt.
 */
function hostnameOf(baseUrl: string): string {
  const parse = (candidate: string): string => {
    try {
      return new URL(candidate).hostname.toLowerCase();
    } catch {
      return '';
    }
  };
  return parse(baseUrl) || parse(`https://${baseUrl}`);
}

/**
 * Name the meeting platform from the captured tab's base URL.
 *
 * Matching is on the parsed hostname rather than on a substring of the URL, so
 * that a host which merely contains a platform's name is not attributed to it.
 * An unrecognised URL is left as `UNKNOWN_PLATFORM` rather than guessed at.
 */
export function platformFromBaseUrl(baseUrl: string | undefined | null): string {
  if (!baseUrl) {
    return UNKNOWN_PLATFORM;
  }
  const hostname = hostnameOf(baseUrl);
  if (!hostname) {
    return UNKNOWN_PLATFORM;
  }
  const match = MEETING_PLATFORM_HOSTS.find(({ exact = [], suffix = [] }) => (
    exact.indexOf(hostname) !== -1
    || suffix.some((host) => hostname === host || hostname.endsWith(`.${host}`))
  ));
  return match ? match.platform : UNKNOWN_PLATFORM;
}

/**
 * The timestamp appended to a meeting's name to make its call id unique.
 *
 * Every field is zero-padded to a fixed width, so ids sort chronologically as
 * plain strings — which is what the meeting list relies on. The clock is a
 * parameter so this is deterministic; the caller passes `new Date()`.
 */
export function formatTimestamp(now: Date): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0'); // JavaScript months start at 0
  const day = String(now.getDate()).padStart(2, '0');
  const hour = String(now.getHours()).padStart(2, '0');
  const minute = String(now.getMinutes()).padStart(2, '0');
  const second = String(now.getSeconds()).padStart(2, '0');
  const millisecond = String(now.getMilliseconds()).padStart(3, '0');
  return `${year}-${month}-${day}-${hour}:${minute}:${second}.${millisecond}`;
}
