/**
 * The blank camera answers camera requests without the avatar, passes the
 * microphone through, and steps aside for the Simli avatar and a real camera.
 *
 * The decision functions are tested directly; the in-page installer is run
 * against a fake window, both as the TypeScript function and as the exact
 * source given to page.addInitScript.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';

import {
    BLANK_CAMERA_DEVICE_ID,
    BLANK_CAMERA_LABEL,
    SIMLI_OVERRIDE_MARKER,
    blankCameraInitScript,
    blankVideoSize,
    cameraToggleState,
    installBlankCameraInPage,
    planMediaRequest,
    shouldListBlankCamera,
} from './blank-camera.js';

const none = { avatarOverrideActive: false, realCameraPresent: false };

// ---- decisions ----

test('an audio-only request is passed through unchanged', () => {
    assert.equal(planMediaRequest({ audio: true }, none), 'passthrough');
    assert.equal(planMediaRequest({ audio: { echoCancellation: false }, video: false }, none), 'passthrough');
    assert.equal(planMediaRequest(undefined, none), 'passthrough');
});

test('a camera+microphone request gets real audio plus a blank video track', () => {
    assert.equal(planMediaRequest({ audio: true, video: true }, none), 'audio-plus-blank');
    assert.equal(planMediaRequest({ audio: {}, video: { width: 1280 } }, none), 'audio-plus-blank');
});

test('a camera-only request gets only a blank video track', () => {
    assert.equal(planMediaRequest({ video: true }, none), 'blank-only');
    assert.equal(planMediaRequest({ audio: false, video: {} }, none), 'blank-only');
});

test('every request is passed through while the Simli override is installed', () => {
    const ctx = { avatarOverrideActive: true, realCameraPresent: false };
    assert.equal(planMediaRequest({ audio: true, video: true }, ctx), 'passthrough');
    assert.equal(planMediaRequest({ video: true }, ctx), 'passthrough');
});

test('every request is passed through when the browser has a real camera', () => {
    const ctx = { avatarOverrideActive: false, realCameraPresent: true };
    assert.equal(planMediaRequest({ audio: true, video: true }, ctx), 'passthrough');
});

test('the blank camera is listed only when there is no camera and no avatar override', () => {
    const mic = [{ kind: 'audioinput' }, { kind: 'audiooutput' }];
    assert.equal(shouldListBlankCamera(mic, false), true);
    assert.equal(shouldListBlankCamera([], false), true);
    assert.equal(shouldListBlankCamera([...mic, { kind: 'videoinput' }], false), false);
    assert.equal(shouldListBlankCamera(mic, true), false);
});

test('the blank track honours the requested size and keeps a low frame rate', () => {
    assert.deepEqual(blankVideoSize(true), { width: 640, height: 360, frameRate: 5 });
    assert.deepEqual(blankVideoSize({}), { width: 640, height: 360, frameRate: 5 });
    assert.deepEqual(
        blankVideoSize({ width: { exact: 1920 }, height: { exact: 1080 }, frameRate: { exact: 30 } }),
        { width: 1920, height: 1080, frameRate: 15 },
    );
    assert.deepEqual(blankVideoSize({ width: { ideal: 1280 }, height: { ideal: 720 } }), {
        width: 1280,
        height: 720,
        frameRate: 5,
    });
    assert.deepEqual(blankVideoSize({ width: 320 }), { width: 320, height: 180, frameRate: 5 });
    assert.deepEqual(blankVideoSize({ width: { max: 4096 }, height: 9999 }), { width: 1920, height: 1080, frameRate: 5 });
    assert.deepEqual(blankVideoSize({ frameRate: { ideal: 0.1 } }), { width: 640, height: 360, frameRate: 1 });
});

test('the pre-join camera toggle state is read from its attributes', () => {
    assert.equal(cameraToggleState({ ariaChecked: 'true' }), 'on');
    assert.equal(cameraToggleState({ ariaChecked: 'false' }), 'off');
    assert.equal(cameraToggleState({ ariaPressed: 'true' }), 'on');
    assert.equal(cameraToggleState({ checked: false }), 'off');
    assert.equal(cameraToggleState({ checked: true }), 'on');
    assert.equal(cameraToggleState({ ariaLabel: 'Turn camera off' }), 'on');
    assert.equal(cameraToggleState({ title: 'Turn camera on' }), 'off');
    assert.equal(cameraToggleState({ ariaChecked: null, checked: null, ariaLabel: 'Camera' }), 'unknown');
    assert.equal(cameraToggleState({}), 'unknown');
});

// ---- the in-page installer against a fake window ----

class FakeTrack {
    readyState = 'live';
    constructor(
        readonly kind: string,
        readonly id: string,
    ) {}
    stop(): void {
        this.readyState = 'ended';
    }
    getSettings(): Record<string, unknown> {
        return { width: 640 };
    }
}

class FakeStream {
    constructor(readonly tracks: FakeTrack[]) {}
    getAudioTracks(): FakeTrack[] {
        return this.tracks.filter((t) => t.kind === 'audio');
    }
    getVideoTracks(): FakeTrack[] {
        return this.tracks.filter((t) => t.kind === 'video');
    }
}

function fakeWindow(opts: { devices?: Array<{ kind: string; deviceId: string }>; nativeVideoFails?: boolean } = {}) {
    const calls: unknown[] = [];
    const intervals: Array<() => void> = [];
    const canvases: Array<{ width: number; height: number; fps?: number; track?: FakeTrack }> = [];
    const devices = opts.devices ?? [
        { kind: 'audioinput', deviceId: 'pulse' },
        { kind: 'audiooutput', deviceId: 'sink' },
    ];
    const win: any = {
        location: { href: 'https://teams.microsoft.com/light-meetings/launch' },
        MediaStream: FakeStream,
        setInterval: (fn: () => void) => {
            intervals.push(fn);
            return intervals.length;
        },
        clearInterval: () => undefined,
        document: {
            createElement: (tag: string) => {
                assert.equal(tag, 'canvas');
                const c: any = {
                    width: 0,
                    height: 0,
                    getContext: () => ({ fillStyle: '', fillRect: () => undefined }),
                    captureStream: (fps: number) => {
                        c.fps = fps;
                        c.track = new FakeTrack('video', `canvas-${canvases.indexOf(c)}`);
                        return new FakeStream([c.track]);
                    },
                };
                canvases.push(c);
                return c;
            },
        },
        navigator: {
            mediaDevices: {
                async getUserMedia(constraints: any) {
                    calls.push(constraints);
                    if (constraints.video) {
                        const err = new Error('Requested device not found');
                        err.name = 'NotFoundError';
                        throw err;
                    }
                    return new FakeStream([new FakeTrack('audio', 'pulse-mic')]);
                },
                async enumerateDevices() {
                    return devices.map((d) => ({ ...d }));
                },
            },
        },
    };
    return { win, calls, canvases, intervals };
}

const deps = {
    plan: planMediaRequest,
    listCamera: shouldListBlankCamera,
    size: blankVideoSize,
    deviceId: BLANK_CAMERA_DEVICE_ID,
    label: BLANK_CAMERA_LABEL,
    marker: SIMLI_OVERRIDE_MARKER,
};

test('a combined request returns the real microphone track and a blank camera track', async () => {
    const { win, calls, canvases } = fakeWindow();
    installBlankCameraInPage(win, deps);
    const audio = { echoCancellation: false };
    const stream = await win.navigator.mediaDevices.getUserMedia({ audio, video: { width: 1280, height: 720 } });
    // The browser was asked for the audio alone, with its constraints unchanged.
    assert.deepEqual(calls, [{ audio }]);
    assert.deepEqual(stream.tracks.map((t: FakeTrack) => t.id), ['pulse-mic', 'canvas-0']);
    assert.equal(canvases[0].width, 1280);
    assert.equal(canvases[0].height, 720);
    assert.equal(stream.getVideoTracks()[0].getSettings().deviceId, BLANK_CAMERA_DEVICE_ID);
    assert.ok(win.__lmaAudio.some((l: string) => l.includes('blank video track')));
});

test('an audio-only request reaches the browser untouched and returns its stream', async () => {
    const { win, calls, canvases } = fakeWindow();
    installBlankCameraInPage(win, deps);
    const constraints = { audio: { channelCount: 2 } };
    const stream = await win.navigator.mediaDevices.getUserMedia(constraints);
    assert.equal(calls[0], constraints);
    assert.deepEqual(stream.tracks.map((t: FakeTrack) => t.id), ['pulse-mic']);
    assert.equal(canvases.length, 0);
});

test('a camera-only request does not touch the microphone', async () => {
    const { win, calls } = fakeWindow();
    installBlankCameraInPage(win, deps);
    const stream = await win.navigator.mediaDevices.getUserMedia({ video: true });
    assert.deepEqual(calls, []);
    assert.deepEqual(stream.tracks.map((t: FakeTrack) => t.kind), ['video']);
});

test('the repaint timer stops once the caller stops the blank track', async () => {
    const { win, canvases, intervals } = fakeWindow();
    let cleared = 0;
    win.clearInterval = () => {
        cleared++;
    };
    installBlankCameraInPage(win, deps);
    await win.navigator.mediaDevices.getUserMedia({ video: true });
    intervals[0]();
    assert.equal(cleared, 0);
    canvases[0].track!.stop();
    intervals[0]();
    assert.equal(cleared, 1);
});

test('a failed microphone request fails the combined request and releases the blank track', async () => {
    const { win, canvases } = fakeWindow();
    win.navigator.mediaDevices.getUserMedia = async () => {
        const err = new Error('denied');
        err.name = 'NotAllowedError';
        throw err;
    };
    installBlankCameraInPage(win, deps);
    await assert.rejects(win.navigator.mediaDevices.getUserMedia({ audio: true, video: true }), { name: 'NotAllowedError' });
    assert.equal(canvases[0].track!.readyState, 'ended');
});

test('enumerateDevices lists the blank camera alongside the real devices', async () => {
    const { win } = fakeWindow();
    installBlankCameraInPage(win, deps);
    const devices = await win.navigator.mediaDevices.enumerateDevices();
    assert.deepEqual(devices.map((d: any) => d.kind), ['audioinput', 'audiooutput', 'videoinput']);
    assert.equal(devices[2].deviceId, BLANK_CAMERA_DEVICE_ID);
    assert.equal(devices[2].label, BLANK_CAMERA_LABEL);
    assert.deepEqual(JSON.parse(JSON.stringify(devices[2])), {
        deviceId: BLANK_CAMERA_DEVICE_ID,
        groupId: BLANK_CAMERA_DEVICE_ID,
        kind: 'videoinput',
        label: BLANK_CAMERA_LABEL,
    });
});

test('with the Simli override in the document, requests and devices are left to it', async () => {
    const { win, calls, canvases } = fakeWindow();
    installBlankCameraInPage(win, deps);
    // The Simli init script may run before or after this one; it marks the
    // window either way, and the shim checks at call time.
    win[SIMLI_OVERRIDE_MARKER] = false;
    await assert.rejects(win.navigator.mediaDevices.getUserMedia({ audio: true, video: true }), {
        name: 'NotFoundError',
    });
    assert.deepEqual(calls, [{ audio: true, video: true }]);
    assert.equal(canvases.length, 0);
    const devices = await win.navigator.mediaDevices.enumerateDevices();
    assert.equal(devices.some((d: any) => d.kind === 'videoinput'), false);
});

test('with a real camera, camera requests go to the browser', async () => {
    const { win, calls, canvases } = fakeWindow({
        devices: [
            { kind: 'audioinput', deviceId: 'mic' },
            { kind: 'videoinput', deviceId: 'usb-cam' },
        ],
    });
    win.navigator.mediaDevices.getUserMedia = async (c: any) => {
        calls.push(c);
        return new FakeStream([new FakeTrack('video', 'usb-cam')]);
    };
    installBlankCameraInPage(win, deps);
    const stream = await win.navigator.mediaDevices.getUserMedia({ video: true });
    assert.deepEqual(stream.tracks.map((t: FakeTrack) => t.id), ['usb-cam']);
    assert.equal(canvases.length, 0);
    assert.equal((await win.navigator.mediaDevices.enumerateDevices()).length, 2);
});

test('installing twice in one frame wraps the browser once', async () => {
    const { win, calls } = fakeWindow();
    installBlankCameraInPage(win, deps);
    const wrapped = win.navigator.mediaDevices.getUserMedia;
    installBlankCameraInPage(win, deps);
    assert.equal(win.navigator.mediaDevices.getUserMedia, wrapped);
    await win.navigator.mediaDevices.getUserMedia({ audio: true, video: true });
    assert.equal(calls.length, 1);
});

test('a frame without mediaDevices is left alone', () => {
    const win: any = { navigator: {} };
    assert.doesNotThrow(() => installBlankCameraInPage(win, deps));
    assert.equal(win.__lmaBlankCameraInstalled, undefined);
});

test('the init script source runs on its own and installs the same shim', async () => {
    const { win, calls } = fakeWindow();
    const sandbox: any = { window: win };
    vm.runInNewContext(blankCameraInitScript(), sandbox);
    assert.equal(win.__lmaBlankCameraInstalled, true);
    const stream = await win.navigator.mediaDevices.getUserMedia({ audio: true, video: true });
    // The sandbox builds objects with its own prototypes, so compare as JSON.
    assert.equal(JSON.stringify(calls), JSON.stringify([{ audio: true }]));
    assert.equal(stream.tracks.map((t: FakeTrack) => t.kind).join(','), 'audio,video');
    const devices = await win.navigator.mediaDevices.enumerateDevices();
    assert.equal(devices.filter((d: any) => d.kind === 'videoinput').length, 1);
});
