/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-var-requires */
import fs from 'fs';
import path from 'path';

// A plain script in the extension's public folder, loaded in the meeting tab and
// in the recorder page; see its header.
const RECORDER_DIR = path.join(__dirname, '../../public/content_scripts/recorder');
const meetingTab = require('../../public/content_scripts/recorder/meeting-tab.js');

const EXTENSION = 'chrome-extension://abcdefghijklmnopabcdefghijklmnop';

type Listener = (event: { source: unknown, origin: string, data: unknown }) => void;

/** A window that records what is posted to it and dispatches what tests deliver. */
const fakeWindow = (extra: Record<string, unknown> = {}) => {
  const listeners: Listener[] = [];
  const posted: { data: unknown, target: string }[] = [];
  const win: any = {
    addEventListener: (type: string, fn: Listener) => { if (type === 'message') listeners.push(fn); },
    removeEventListener: (type: string, fn: Listener) => {
      const at = listeners.indexOf(fn);
      if (at >= 0) listeners.splice(at, 1);
    },
    postMessage: (data: unknown, target: string) => posted.push({ data, target }),
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (t: ReturnType<typeof setTimeout>) => clearTimeout(t),
    deliver: (event: { source: unknown, origin: string, data: unknown }) => [...listeners].forEach((fn) => fn(event)),
    listeners,
    posted,
    ...extra,
  };
  return win;
};

/** A video track as getDisplayMedia returns one. */
const track = (surface: string | undefined, captured: { handle: string } | null | 'none') => ({
  getSettings: () => (surface === undefined ? {} : { displaySurface: surface }),
  ...(captured === 'none' ? {} : { getCaptureHandle: () => captured }),
});

describe('the meeting tab check on a shared track', () => {
  test('passes the meeting tab, which carries the label the recorder chose', () => {
    expect(meetingTab.lmaMeetingTabCheck(track('browser', { handle: 'label-1' }), 'label-1')).toBeNull();
  });

  test('refuses another tab, which carries a label of its own or none', () => {
    expect(meetingTab.lmaMeetingTabCheck(track('browser', { handle: 'label-2' }), 'label-1')).toBe('not-meeting-tab');
    expect(meetingTab.lmaMeetingTabCheck(track('browser', null), 'label-1')).toBe('not-meeting-tab');
  });

  test('refuses a window or a screen whatever it carries', () => {
    expect(meetingTab.lmaMeetingTabCheck(track('window', { handle: 'label-1' }), 'label-1')).toBe('not-meeting-tab');
    expect(meetingTab.lmaMeetingTabCheck(track('monitor', null), 'label-1')).toBe('not-meeting-tab');
    expect(meetingTab.lmaMeetingTabCheck(undefined, 'label-1')).toBe('not-meeting-tab');
  });

  test('treats a tab it cannot check as unconfirmed, never as the meeting tab', () => {
    expect(meetingTab.lmaMeetingTabCheck(track('browser', 'none'), 'label-1')).toBe('unverified');
    expect(meetingTab.lmaMeetingTabCheck(track('browser', { handle: 'label-1' }), null)).toBe('unverified');
  });
});

describe('labelling the meeting tab', () => {
  const setup = (setCaptureHandleConfig: (config: unknown) => void) => {
    const recorder = fakeWindow();
    const frame = { contentWindow: recorder };
    const tab = fakeWindow({ navigator: { mediaDevices: { setCaptureHandleConfig } } });
    recorder.parent = tab;
    meetingTab.lmaAnswerCaptureHandleRequests(tab, frame, EXTENSION);
    // What the recorder posts reaches the tab; what the tab posts reaches the recorder.
    tab.postMessage = (data: unknown) => tab.deliver({ source: recorder, origin: EXTENSION, data });
    recorder.postMessage = (data: unknown, target: string) => {
      expect(target).toBe(EXTENSION);
      recorder.deliver({ source: tab, origin: 'https://meeting.example', data });
    };
    return { recorder, tab };
  };

  test('labels the tab for the extension only, and resolves to the label', async () => {
    const configs: unknown[] = [];
    const { recorder } = setup((config) => configs.push(config));
    const label = await meetingTab.lmaLabelMeetingTab(recorder, { newLabel: () => 'label-1' });
    expect(label).toBe('label-1');
    expect(configs).toEqual([{ handle: 'label-1', exposeOrigin: false, permittedOrigins: [EXTENSION] }]);
    expect(recorder.listeners).toHaveLength(0);
  });

  test('resolves to null when the browser cannot label a tab', async () => {
    const { recorder } = setup(() => { throw new Error('not supported'); });
    expect(await meetingTab.lmaLabelMeetingTab(recorder, { newLabel: () => 'label-1' })).toBeNull();
  });

  test('resolves to null when nothing answers in time', async () => {
    const recorder = fakeWindow();
    recorder.parent = fakeWindow();
    expect(await meetingTab.lmaLabelMeetingTab(recorder, { waitMs: 10, newLabel: () => 'label-1' })).toBeNull();
    expect(recorder.listeners).toHaveLength(0);
  });

  test('ignores answers that are not from the meeting tab or not for its label', async () => {
    const recorder = fakeWindow();
    recorder.parent = fakeWindow();
    const pending = meetingTab.lmaLabelMeetingTab(recorder, { waitMs: 50, newLabel: () => 'label-1' });
    recorder.deliver({ source: {}, origin: EXTENSION, data: { lma: meetingTab.LMA_CAPTURE_HANDLE_SET, handle: 'label-1', ok: true } });
    recorder.deliver({ source: recorder.parent, origin: EXTENSION, data: { lma: meetingTab.LMA_CAPTURE_HANDLE_SET, handle: 'label-2', ok: true } });
    expect(await pending).toBeNull();
  });

  test('the tab labels itself only for its own recorder, at the extension origin', () => {
    const configs: unknown[] = [];
    const tab = fakeWindow({ navigator: { mediaDevices: { setCaptureHandleConfig: (c: unknown) => configs.push(c) } } });
    const recorder = fakeWindow();
    meetingTab.lmaAnswerCaptureHandleRequests(tab, { contentWindow: recorder }, EXTENSION);
    const request = { lma: meetingTab.LMA_SET_CAPTURE_HANDLE, handle: 'label-1' };
    tab.deliver({ source: tab, origin: 'https://meeting.example', data: request });
    tab.deliver({ source: recorder, origin: 'https://meeting.example', data: request });
    tab.deliver({ source: recorder, origin: EXTENSION, data: { lma: 'something-else', handle: 'label-1' } });
    expect(configs).toEqual([]);
    expect(recorder.posted).toEqual([]);
    tab.deliver({ source: recorder, origin: EXTENSION, data: request });
    expect(configs).toHaveLength(1);
    expect(recorder.posted).toEqual([{ data: { lma: meetingTab.LMA_CAPTURE_HANDLE_SET, handle: 'label-1', ok: true }, target: EXTENSION }]);
  });
});

describe('wiring', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../../public/manifest.json'), 'utf8'));

  test('the meeting tab loads the check before the script that injects the recorder', () => {
    const script = manifest.content_scripts.find((s: { js?: string[] }) => (s.js || []).includes('content_scripts/recorder/inject-recorder.js'));
    expect(script.js).toEqual(['content_scripts/recorder/meeting-tab.js', 'content_scripts/recorder/inject-recorder.js']);
    // In the top-level document only, the one place a tab can be labelled.
    expect(script.all_frames).toBeFalsy();
    const inject = fs.readFileSync(path.join(RECORDER_DIR, 'inject-recorder.js'), 'utf8');
    expect(inject).toContain('lmaAnswerCaptureHandleRequests(window, iframe, new URL(chrome.runtime.getURL("")).origin);');
  });

  test('the recorder page loads the check before the recorder, and the recorder uses it', () => {
    const page = fs.readFileSync(path.join(RECORDER_DIR, 'recorder.html'), 'utf8');
    expect(page.indexOf('src="meeting-tab.js"')).toBeGreaterThan(-1);
    expect(page.indexOf('src="meeting-tab.js"')).toBeLessThan(page.indexOf('src="recorder.js"'));
    const recorder = fs.readFileSync(path.join(RECORDER_DIR, 'recorder.js'), 'utf8');
    expect(recorder).toContain('await lmaLabelMeetingTab(window)');
    expect(recorder).toContain('lmaMeetingTabCheck(track, meetingTabLabel)');
    expect(recorder).not.toContain('displaySurface');
  });
});
