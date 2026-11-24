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
 * fragmented-MP4 chunks and END_VIDEO (videorecording.ts). One rule holds for
 * every client, whatever client type it declares: video is recorded when the
 * deployment records video (the EnableVideoRecording stack parameter) and
 * Meeting Recording › Record screen video from streaming clients is on, as the
 * meeting's audio START read it. On a deployment that does not record video a
 * START_VIDEO is accepted and discarded, whatever the setting, as it always has
 * been. On one that does, with the setting off, a client that declares it
 * handles a refusal (START_VIDEO's handlesRefusal) is refused; any other client
 * has its stream accepted and discarded, so a client that reconnects after
 * every close it did not start keeps one quiet connection. No video is kept in
 * either case. The client type names only what the key frames are
 * recorded as (keyframeSourceFor).
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

/**
 * The frame sent on a video connection once its START_VIDEO has a video
 * session on this server: from then on a dropped connection resumes the
 * session (videoResume), rather than starting a stream again.
 */
export const videoStartedFrame = (callId: string): Record<string, unknown> => ({ event: 'VIDEO_STARTED', callId });

/** What took a key frame streamed to this server; see docs/meeting-video.md. */
export type StreamedKeyframeSource = 'DESKTOP_CAPTURE' | 'STREAM_AUDIO' | 'BROWSER_EXTENSION';

export interface VideoSwitches {
    /** The EnableVideoRecording stack parameter (ENABLE_VIDEO_RECORDING). */
    readonly stackRecordsVideo: boolean;
    /** Meeting Recording › Record screen video from streaming clients. */
    readonly recordScreenVideo: boolean;
}

/**
 * Whether a streaming client may record screen video: the same answer for
 * every client, so the client type a client declares plays no part in it.
 */
export const videoAllowed = (switches: VideoSwitches): boolean =>
    switches.stackRecordsVideo && switches.recordScreenVideo;

/**
 * What becomes of a START_VIDEO; see the module comment. `discard`: the
 * deployment records no video. `withhold`: the setting is off and the client
 * did not declare that it handles a refusal, so its stream is accepted and
 * discarded as well.
 */
export type VideoAdmission = 'record' | 'discard' | 'withhold' | 'refuse';

/**
 * How a START_VIDEO is answered: accepted and discarded (no VIDEO_STARTED) on a
 * deployment that does not record video, whatever the setting; with the setting
 * off, refused (VIDEO_NOT_ENABLED_CLOSE) when the client declares it handles a
 * refusal and accepted and discarded when it does not; and recorded otherwise.
 * What the client declares changes only how it is told, never whether video is
 * kept, and its client type plays no part.
 */
export const videoAdmission = (switches: VideoSwitches, handlesRefusal: boolean): VideoAdmission => {
    if (!switches.stackRecordsVideo) {
        return 'discard';
    }
    if (switches.recordScreenVideo) {
        return 'record';
    }
    return handlesRefusal ? 'refuse' : 'withhold';
};

/**
 * The key frame Source of a client's video: the client's own name for the two
 * browser clients, and DESKTOP_CAPTURE for the Desktop Capture App and the
 * streaming API, as before browser clients sent video. An undefined capture
 * source is treated as the streaming API's. A label only: it decides nothing
 * about whether the video is recorded.
 */
export const keyframeSourceFor = (source: StreamingCaptureSource | undefined): StreamedKeyframeSource =>
    source === 'STREAM_AUDIO' || source === 'BROWSER_EXTENSION' ? source : 'DESKTOP_CAPTURE';

/** What became of a chunk offered to {@link PendingVideoChunks.hold}. */
export type VideoChunkHoldResult = 'held' | 'full' | 'no-start';

/**
 * Video chunks that arrive while the START_VIDEO before them is still being
 * handled.
 *
 * A client sends its first chunks, the initialization segment first, straight
 * behind START_VIDEO (docs/websocket-streaming-api.md, "6.1.6 Screen Video").
 * The socket hands each frame to its handler as it arrives, without waiting for
 * the handler of the frame before it, and START_VIDEO has its connection's
 * video session only after an await, so a chunk right behind it would find no
 * session. Chunks for a connection whose START_VIDEO is in progress are held
 * here and handed back, in order, once it has a session. At most `limitBytes`
 * are held per connection: the most a browser client keeps before its stream
 * is confirmed.
 */
export class PendingVideoChunks<K> {
    private readonly pending = new Map<K, { chunks: Uint8Array[], bytes: number }>();

    private readonly limitBytes: number;

    constructor(limitBytes = 32 * 1024 * 1024) {
        this.limitBytes = limitBytes;
    }

    /** A START_VIDEO began on this connection: hold its chunks until it has finished. */
    begin(key: K): void {
        this.pending.set(key, { chunks: [], bytes: 0 });
    }

    /** Hold a chunk for a START_VIDEO in progress on this connection. */
    hold(key: K, chunk: Uint8Array): VideoChunkHoldResult {
        const held = this.pending.get(key);
        if (!held) {
            return 'no-start';
        }
        if (held.bytes + chunk.length > this.limitBytes) {
            return 'full';
        }
        held.chunks.push(chunk);
        held.bytes += chunk.length;
        return 'held';
    }

    /** The START_VIDEO has a session: the chunks held for it, in the order they arrived. */
    release(key: K): Uint8Array[] {
        const chunks = this.pending.get(key)?.chunks ?? [];
        this.pending.delete(key);
        return chunks;
    }

    /** The START_VIDEO was refused or the connection closed: forget its chunks. */
    drop(key: K): void {
        this.pending.delete(key);
    }
}
