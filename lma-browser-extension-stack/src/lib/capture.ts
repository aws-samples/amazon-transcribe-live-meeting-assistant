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
 * Name the meeting platform from the captured tab's base URL.
 *
 * Teams is matched by substring across its four hostnames, and the others by the
 * exact origin they use, which is how the original distinguished them. An
 * unrecognised URL is left as `UNKNOWN_PLATFORM` rather than guessed at.
 *
 * Google Meet belongs here even though it is not a Virtual Participant platform:
 * the extension is how Meet meetings are captured.
 */
export function platformFromBaseUrl(baseUrl: string | undefined | null): string {
  if (!baseUrl) {
    return UNKNOWN_PLATFORM;
  }
  if (baseUrl === 'https://app.zoom.us') {
    return 'Zoom';
  }
  if (baseUrl === 'https://app.chime.aws') {
    return 'Amazon Chime';
  }
  if (
    baseUrl.includes('teams.microsoft.com') ||
    baseUrl.includes('teams.live.com') ||
    baseUrl.includes('teams.microsoft.us') ||
    baseUrl.includes('teams.cloud.microsoft')
  ) {
    return 'Microsoft Teams';
  }
  if (baseUrl.includes('webex.com')) {
    return 'Cisco Webex';
  }
  if (baseUrl === 'https://meet.google.com') {
    return 'Google Meet';
  }
  return UNKNOWN_PLATFORM;
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
