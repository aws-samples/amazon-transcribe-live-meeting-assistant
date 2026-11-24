/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/*
 * Whether the tab a user shared is the meeting tab: the tab whose recorder
 * asked for the share. Screen video is recorded only when it is.
 *
 * The check uses the Capture Handle API, a web standard
 * (https://w3c.github.io/mediacapture-handle/identity/): a page labels itself
 * with navigator.mediaDevices.setCaptureHandleConfig(), and whoever captures
 * it reads that label from the captured video track (track.getCaptureHandle()).
 * Before asking for the share, the recorder chooses a new random label and
 * asks the extension's script in the meeting tab's top-level document to label
 * the tab with it. The API can be called only from a top-level document, and
 * the recorder runs in an iframe. The label is visible only to this extension's
 * own origin. After the share, the label on the track is compared with the one
 * the recorder chose. Another tab carries a different label, or none, and a
 * window or a screen carries none.
 *
 * Other ways were considered and not used. displaySurface says only that some
 * browser tab was shared, not which one. chrome.tabCapture identifies the tab
 * by construction, but it would replace the share dialog and the way the audio
 * is captured.
 *
 * Anything the check cannot confirm counts as not the meeting tab, and the
 * meeting records audio only.
 *
 * Loaded twice, as plain scripts: in the meeting tab's top-level document, as a
 * content script before inject-recorder.js (answerCaptureHandleRequests), and
 * in the recorder page before recorder.js (labelMeetingTab, meetingTabCheck).
 * src/lib/meeting-tab.test.ts tests it through module.exports.
 */

/** The message the recorder posts to the meeting tab, and the answer to it. */
const LMA_SET_CAPTURE_HANDLE = 'lma-set-capture-handle';
const LMA_CAPTURE_HANDLE_SET = 'lma-capture-handle-set';

/** How long the recorder waits for the meeting tab to be labelled. */
const LMA_CAPTURE_HANDLE_WAIT_MS = 1000;

/**
 * In the meeting tab's top-level document: label the tab for the recorder in
 * `frame`, when that recorder asks. Only messages from that frame, at the
 * extension's own origin, are answered. Returns the listener, for tests.
 */
const lmaAnswerCaptureHandleRequests = (win, frame, extensionOrigin) => {
  const listener = (event) => {
    const data = event.data;
    if (event.source !== frame.contentWindow || event.origin !== extensionOrigin) return;
    if (!data || data.lma !== LMA_SET_CAPTURE_HANDLE || typeof data.handle !== 'string') return;
    let ok = false;
    try {
      win.navigator.mediaDevices.setCaptureHandleConfig({
        handle: data.handle,
        exposeOrigin: false,
        permittedOrigins: [extensionOrigin],
      });
      ok = true;
    } catch (error) {
      // Not supported in this browser: the recorder records audio only.
      console.log('LMA: could not label the meeting tab for screen video', String(error));
    }
    frame.contentWindow.postMessage({ lma: LMA_CAPTURE_HANDLE_SET, handle: data.handle, ok }, extensionOrigin);
  };
  win.addEventListener('message', listener);
  return listener;
};

/**
 * In the recorder: have the meeting tab labelled with a new random label.
 * Resolves to the label, or to null when the tab could not be labelled or did
 * not answer in time.
 */
const lmaLabelMeetingTab = (win, { waitMs = LMA_CAPTURE_HANDLE_WAIT_MS, newLabel } = {}) =>
  new Promise((resolve) => {
    const handle = newLabel ? newLabel() : win.crypto.randomUUID();
    let timer = null;
    const listener = (event) => {
      const data = event.data;
      if (event.source !== win.parent || !data || data.lma !== LMA_CAPTURE_HANDLE_SET || data.handle !== handle) return;
      win.clearTimeout(timer);
      win.removeEventListener('message', listener);
      resolve(data.ok === true ? handle : null);
    };
    win.addEventListener('message', listener);
    timer = win.setTimeout(() => {
      win.removeEventListener('message', listener);
      resolve(null);
    }, waitMs);
    // The meeting page's origin is not known here; the label is not a secret
    // from the meeting tab, which is the tab it labels.
    win.parent.postMessage({ lma: LMA_SET_CAPTURE_HANDLE, handle }, '*');
  });

/**
 * Whether a shared video track shows the meeting tab, given the label the tab
 * was given (or null). Returns null when it does, or the reason it is not
 * recorded: 'not-meeting-tab' for another tab, a window or a screen, and
 * 'unverified' when this browser cannot tell.
 */
const lmaMeetingTabCheck = (track, handle) => {
  if (!track) return 'not-meeting-tab';
  const surface = typeof track.getSettings === 'function' ? track.getSettings().displaySurface : undefined;
  if (surface !== undefined && surface !== 'browser') return 'not-meeting-tab';
  if (!handle || typeof track.getCaptureHandle !== 'function') return 'unverified';
  const captured = track.getCaptureHandle();
  return captured && captured.handle === handle ? null : 'not-meeting-tab';
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    LMA_SET_CAPTURE_HANDLE,
    LMA_CAPTURE_HANDLE_SET,
    LMA_CAPTURE_HANDLE_WAIT_MS,
    lmaAnswerCaptureHandleRequests,
    lmaLabelMeetingTab,
    lmaMeetingTabCheck,
  };
}
