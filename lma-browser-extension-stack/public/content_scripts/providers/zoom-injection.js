/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Zoom publishes the meeting number, topic and display name on a page global.
// It appears some seconds after the meeting page loads, so it has to be polled
// for — but the poll is bounded: on a zoom.us page that is not a meeting, or on
// a Zoom web client that no longer sets this global, an unbounded poll logs once
// a second for the life of the tab and never succeeds. The Zoom reader reports
// the platform from the tab's own origin regardless of what happens here.
const MEETING_CONFIG_POLL_MS = 1000;
const MEETING_CONFIG_MAX_ATTEMPTS = 120; // two minutes

let meetingConfigAttempts = 0;

const checkVariable = setInterval(() => {
  if (typeof MeetingConfig !== 'undefined') {
    console.log('meeting config defined:', MeetingConfig);
    window.postMessage({ type: "MeetingConfig", value: MeetingConfig });
    clearInterval(checkVariable);
    return;
  }
  meetingConfigAttempts += 1;
  if (meetingConfigAttempts >= MEETING_CONFIG_MAX_ATTEMPTS) {
    console.log(`MeetingConfig not defined after ${meetingConfigAttempts} attempts. Giving up; the meeting name and topic must be entered by hand.`);
    clearInterval(checkVariable);
    return;
  }
  console.log(`MeetingConfig not yet defined (attempt ${meetingConfigAttempts}/${MEETING_CONFIG_MAX_ATTEMPTS}).`);
}, MEETING_CONFIG_POLL_MS);
