/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which streaming clients may send screen video, and what their key frames
 * are recorded as.
 *
 * Every client sends video the same way: a second connection with START_VIDEO,
 * fragmented-MP4 chunks and END_VIDEO (videorecording.ts). The Desktop Capture
 * App, and any client of the streaming API that does not name itself, may do so
 * whenever the deployment records video (the EnableVideoRecording stack
 * parameter). The Stream Audio/Video page and the browser extension may do so
 * only when Meeting Recording › Record screen video from Stream Audio/Video and
 * the browser extension is on as well, as the meeting's audio START read it.
 *
 * Kept free of AWS clients so it can be tested directly.
 */
import type { StreamingCaptureSource } from './calleventdata/capture-context';

/**
 * How a video connection is closed when its client may not send video. The
 * client stops its video and carries on with the audio; see
 * docs/websocket-streaming-api.md, "6.1.6 Screen Video".
 */
export const VIDEO_NOT_ENABLED_CLOSE = { code: 1008, reason: 'screen video is not enabled for this client' } as const;

/** What took a key frame streamed to this server; see docs/meeting-video.md. */
export type StreamedKeyframeSource = 'DESKTOP_CAPTURE' | 'STREAM_AUDIO' | 'BROWSER_EXTENSION';

/** The clients whose video the Meeting Recording setting governs. */
const BROWSER_VIDEO_CLIENTS: ReadonlySet<StreamingCaptureSource> = new Set(['STREAM_AUDIO', 'BROWSER_EXTENSION']);

/** Whether the Meeting Recording setting governs this client's video. */
export const isBrowserVideoClient = (source: StreamingCaptureSource | undefined): boolean =>
    source !== undefined && BROWSER_VIDEO_CLIENTS.has(source);

export interface VideoSwitches {
    /** The EnableVideoRecording stack parameter (ENABLE_VIDEO_RECORDING). */
    readonly stackRecordsVideo: boolean;
    /** Meeting Recording › Record screen video from Stream Audio/Video and the browser extension. */
    readonly recordBrowserVideo: boolean;
}

/**
 * Whether a client of this capture source may record screen video. Undefined
 * is a session that predates capture sources, treated as the streaming API's.
 */
export const videoAllowedFor = (source: StreamingCaptureSource | undefined, switches: VideoSwitches): boolean =>
    switches.stackRecordsVideo && (!isBrowserVideoClient(source) || switches.recordBrowserVideo);

/**
 * The key frame Source of a client's video: the client's own name for the two
 * browser clients, and DESKTOP_CAPTURE for the Desktop Capture App and the
 * streaming API, as before browser clients sent video.
 */
export const keyframeSourceFor = (source: StreamingCaptureSource | undefined): StreamedKeyframeSource =>
    source === 'STREAM_AUDIO' || source === 'BROWSER_EXTENSION' ? source : 'DESKTOP_CAPTURE';
