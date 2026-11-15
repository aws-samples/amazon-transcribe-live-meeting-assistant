/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A blank camera for joins without the Simli avatar (GitHub #1094).
 *
 * The VP's browser has a microphone (a PulseAudio virtual source the voice
 * assistant speaks into) but no camera. Chromium is started with
 * `--use-fake-ui-for-media-stream`, which accepts the permission prompt, but
 * not with `--use-fake-device-for-media-stream`, which would also replace the
 * microphone. So a `getUserMedia({ audio, video })` request fails for lack of
 * a video device. Until now the only code that answered a video request was
 * the Simli avatar's override, installed only when the avatar is connected.
 * Without the avatar, Teams showed "Are you sure you don't want audio or
 * video?" after the join click although its audio-only request had succeeded;
 * the combined request failing is the inferred cause, not a measured one.
 *
 * This module supplies a camera that produces black frames:
 *
 * - an audio request is passed to the browser unchanged;
 * - a request that includes video gets a black canvas track, plus the real
 *   audio track(s) when audio was requested too;
 * - `enumerateDevices` lists one camera when the browser reports none.
 *
 * It steps aside completely when the Simli avatar's override is installed in
 * the same document (that override marks the window with `__simliWantsCamera`
 * when its init script runs), so the avatar path is unchanged whichever order
 * Playwright runs the init scripts in. It also steps aside when the browser has
 * a real camera (a local run on a workstation).
 *
 * The pre-join flows still turn the camera off; this only makes the device
 * exist. The decision logic is plain functions, unit-tested without a browser;
 * `blankCameraInitScript` assembles them with the in-page installer into the
 * source given to `page.addInitScript`, so every frame of the page (including
 * Teams' meeting iframes) gets the shim.
 */

/** The camera listed by enumerateDevices and reported by the blank track. */
export const BLANK_CAMERA_DEVICE_ID = 'lma-blank-camera';
export const BLANK_CAMERA_LABEL = 'LMA Virtual Camera';

/** Window property the Simli avatar's getUserMedia override sets when installed. */
export const SIMLI_OVERRIDE_MARKER = '__simliWantsCamera';

/** What to do with one getUserMedia request. */
export type BlankCameraPlan =
    /** Hand the request to the browser unchanged. */
    | 'passthrough'
    /** Real audio from the browser, plus a blank video track. */
    | 'audio-plus-blank'
    /** Only a blank video track. */
    | 'blank-only';

export interface BlankCameraContext {
    /** The Simli avatar's override is installed in this document. */
    avatarOverrideActive: boolean;
    /** The browser lists a real camera. */
    realCameraPresent: boolean;
}

/**
 * Decide how to answer a getUserMedia request. Self-contained: it is
 * serialised into the page by blankCameraInitScript.
 */
export function planMediaRequest(
    constraints: { audio?: unknown; video?: unknown } | null | undefined,
    ctx: BlankCameraContext,
): BlankCameraPlan {
    if (ctx.avatarOverrideActive || ctx.realCameraPresent) return 'passthrough';
    const video = !!(constraints && constraints.video);
    if (!video) return 'passthrough';
    const audio = !!(constraints && constraints.audio);
    return audio ? 'audio-plus-blank' : 'blank-only';
}

/**
 * Whether enumerateDevices should list the blank camera. Self-contained: it is
 * serialised into the page by blankCameraInitScript.
 */
export function shouldListBlankCamera(
    devices: ReadonlyArray<{ kind: string }>,
    avatarOverrideActive: boolean,
): boolean {
    if (avatarOverrideActive) return false;
    return !devices.some((d) => d.kind === 'videoinput');
}

export interface BlankVideoSize {
    width: number;
    height: number;
    frameRate: number;
}

/**
 * The size and frame rate of the blank track for a request's video
 * constraints. A sender that never gets the size it negotiated for can stall
 * (the ACS calling SDK asks for an exact 1920x1080), so an exact, ideal or min
 * value is honoured; anything else gets 640x360. Frames are black and
 * unchanging, so the frame rate is kept low. Self-contained: it is serialised
 * into the page by blankCameraInitScript.
 */
export function blankVideoSize(video: unknown): BlankVideoSize {
    const pick = (v: unknown): number | undefined => {
        if (typeof v === 'number' && Number.isFinite(v)) return v;
        if (v && typeof v === 'object') {
            const r = v as { exact?: unknown; ideal?: unknown; min?: unknown };
            for (const x of [r.exact, r.ideal, r.min]) {
                if (typeof x === 'number' && Number.isFinite(x)) return x;
            }
        }
        return undefined;
    };
    const clamp = (n: number, lo: number, hi: number): number => Math.round(Math.min(hi, Math.max(lo, n)));
    const c = video && typeof video === 'object' ? (video as Record<string, unknown>) : {};
    const w = pick(c.width);
    const h = pick(c.height);
    const fps = pick(c.frameRate);
    let width = 640;
    let height = 360;
    if (w !== undefined && h !== undefined) {
        width = w;
        height = h;
    } else if (w !== undefined) {
        width = w;
        height = (w * 9) / 16;
    } else if (h !== undefined) {
        width = (h * 16) / 9;
        height = h;
    }
    return {
        width: clamp(width, 16, 1920),
        height: clamp(height, 16, 1080),
        frameRate: clamp(fps ?? 5, 1, 15),
    };
}

/** The decision functions the in-page installer needs. */
export interface BlankCameraDeps {
    plan: typeof planMediaRequest;
    listCamera: typeof shouldListBlankCamera;
    size: typeof blankVideoSize;
    deviceId: string;
    label: string;
    marker: string;
}

/**
 * Runs in the page (and in tests, against a fake window). Wraps
 * navigator.mediaDevices.getUserMedia and enumerateDevices. Self-contained
 * apart from `deps`.
 */
export function installBlankCameraInPage(win: any, deps: BlankCameraDeps): void {
    const md = win.navigator && win.navigator.mediaDevices;
    if (!md || typeof md.getUserMedia !== 'function' || typeof md.enumerateDevices !== 'function') return;
    if (win.__lmaBlankCameraInstalled) return;
    win.__lmaBlankCameraInstalled = true;

    const note = (msg: string): void => {
        // Drained by the [LMA-Audio] diagnostics poller in Node; page
        // console.log does not reach the container log under cloakbrowser.
        try {
            if (!win.__lmaAudio) win.__lmaAudio = [];
            if (win.__lmaAudio.length < 500) win.__lmaAudio.push(`blank camera: ${msg}`);
        } catch {
            /* diagnostics only */
        }
    };

    const originalGetUserMedia = md.getUserMedia.bind(md);
    const originalEnumerate = md.enumerateDevices.bind(md);
    const avatarActive = (): boolean => deps.marker in win;
    const realCamera = async (): Promise<boolean> => {
        try {
            const devices: Array<{ kind: string; deviceId?: string }> = await originalEnumerate();
            return devices.some((d) => d.kind === 'videoinput' && d.deviceId !== deps.deviceId);
        } catch {
            return false;
        }
    };

    const blankTrack = (video: unknown): any => {
        const { width, height, frameRate } = deps.size(video);
        const canvas = win.document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        if (ctx) {
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, width, height);
        }
        const track = canvas.captureStream(frameRate).getVideoTracks()[0];
        // A canvas track only emits a frame when the canvas is painted, and a
        // sender that never gets a frame can stall. Repaint one pixel at the
        // frame rate (setInterval, so a background tab keeps going), and stop
        // once the caller stops the track: stop() does not fire 'ended'.
        let timer: any = 0;
        timer = win.setInterval(() => {
            if (track.readyState !== 'live') {
                win.clearInterval(timer);
                return;
            }
            if (ctx) ctx.fillRect(0, 0, 1, 1);
        }, Math.round(1000 / frameRate));
        // Clients match the track to the device they enumerated.
        try {
            const settings = track.getSettings.bind(track);
            track.getSettings = () => ({ ...settings(), deviceId: deps.deviceId, groupId: deps.deviceId });
        } catch {
            /* settings stay the canvas's own */
        }
        return track;
    };

    md.getUserMedia = async function (constraints?: any): Promise<any> {
        const plan = deps.plan(constraints, {
            avatarOverrideActive: avatarActive(),
            realCameraPresent: constraints && constraints.video ? await realCamera() : false,
        });
        if (plan === 'passthrough') return originalGetUserMedia(constraints);
        const video = blankTrack(constraints.video);
        if (plan === 'blank-only') {
            note('served a blank video track (video-only request)');
            return new win.MediaStream([video]);
        }
        let audio: any;
        try {
            audio = await originalGetUserMedia({ audio: constraints.audio });
        } catch (err) {
            video.stop();
            throw err;
        }
        note('served real audio with a blank video track (audio+video request)');
        return new win.MediaStream([...audio.getAudioTracks(), video]);
    };

    md.enumerateDevices = async function (): Promise<any[]> {
        const devices: any[] = await originalEnumerate();
        if (!deps.listCamera(devices, avatarActive())) return devices;
        const proto = (win.InputDeviceInfo || win.MediaDeviceInfo || Object).prototype;
        const fields = { deviceId: deps.deviceId, groupId: deps.deviceId, kind: 'videoinput', label: deps.label };
        const camera = Object.create(proto, {
            deviceId: { value: fields.deviceId, enumerable: true },
            groupId: { value: fields.groupId, enumerable: true },
            kind: { value: fields.kind, enumerable: true },
            label: { value: fields.label, enumerable: true },
            toJSON: { value: () => ({ ...fields }) },
            getCapabilities: { value: () => ({ deviceId: fields.deviceId, groupId: fields.groupId }) },
        });
        return [...devices, camera];
    };
    note(`installed in frame ${String(win.location && win.location.href).slice(0, 120)}`);
}

/** Source for page.addInitScript: the installer plus the functions it uses. */
export function blankCameraInitScript(): string {
    return `(${installBlankCameraInPage.toString()})(window, {
    plan: ${planMediaRequest.toString()},
    listCamera: ${shouldListBlankCamera.toString()},
    size: ${blankVideoSize.toString()},
    deviceId: ${JSON.stringify(BLANK_CAMERA_DEVICE_ID)},
    label: ${JSON.stringify(BLANK_CAMERA_LABEL)},
    marker: ${JSON.stringify(SIMLI_OVERRIDE_MARKER)},
});`;
}

/** The camera state a pre-join camera toggle shows. */
export type CameraToggleState = 'on' | 'off' | 'unknown';

/**
 * Read a pre-join camera toggle's state from its attributes. With the blank
 * camera present, Teams may show the camera on or off, so the pre-join flow
 * clicks the toggle only when it is on or its state cannot be read (the
 * previous unconditional click), never when it is already off.
 */
export function cameraToggleState(attrs: {
    ariaChecked?: string | null;
    ariaPressed?: string | null;
    checked?: boolean | null;
    ariaLabel?: string | null;
    title?: string | null;
}): CameraToggleState {
    for (const v of [attrs.ariaChecked, attrs.ariaPressed]) {
        if (v === 'true') return 'on';
        if (v === 'false') return 'off';
    }
    if (attrs.checked === true) return 'on';
    if (attrs.checked === false) return 'off';
    const label = `${attrs.ariaLabel || ''} ${attrs.title || ''}`.toLowerCase();
    if (/turn (the )?camera off|turn off (the )?camera|stop video|camera is on/.test(label)) return 'on';
    if (/turn (the )?camera on|turn on (the )?camera|start video|camera is off/.test(label)) return 'off';
    return 'unknown';
}
