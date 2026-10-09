/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Zoom publishes the meeting number, topic and display name on a page global.
// It appears some seconds after the meeting page loads, so it has to be polled
// for — but a page that never defines it (a zoom.us page that is not a meeting,
// or a Zoom web client that no longer sets it) must not be polled at one second
// per second for the life of the tab.
//
// So the poll backs off rather than stopping outright: a fast phase while the
// meeting page is still settling, then a slow one that still catches the global
// if it appears late, which it does when the host keeps everyone in a waiting
// room. Only the first failure and the transitions are logged, instead of one
// line per attempt. The Zoom reader reports the platform from the tab's own
// origin regardless of what happens here.
const FAST_POLL_MS = 1000;
const FAST_POLL_ATTEMPTS = 120; // two minutes
const SLOW_POLL_MS = 15000;
const TOTAL_POLL_MS = 60 * 60 * 1000; // an hour, then the page is not a meeting

let attempts = 0;
let elapsedMs = 0;
let pollMs = FAST_POLL_MS;

const publishMeetingConfig = function () {
  console.log('meeting config defined:', MeetingConfig);
  window.postMessage({ type: "MeetingConfig", value: MeetingConfig });
}

const pollForMeetingConfig = function () {
  if (typeof MeetingConfig !== 'undefined') {
    publishMeetingConfig();
    return;
  }

  attempts += 1;
  elapsedMs += pollMs;
  if (attempts === 1) {
    console.log('MeetingConfig not yet defined; waiting for it.');
  }

  if (elapsedMs >= TOTAL_POLL_MS) {
    console.log(`MeetingConfig never defined after ${Math.round(elapsedMs / 60000)} minutes. Giving up; the meeting name and topic must be entered by hand.`);
    return;
  }

  if (pollMs === FAST_POLL_MS && attempts >= FAST_POLL_ATTEMPTS) {
    pollMs = SLOW_POLL_MS;
    console.log(`MeetingConfig still not defined after ${FAST_POLL_ATTEMPTS} attempts; checking every ${SLOW_POLL_MS / 1000}s from here.`);
  }

  setTimeout(pollForMeetingConfig, pollMs);
}

pollForMeetingConfig();
