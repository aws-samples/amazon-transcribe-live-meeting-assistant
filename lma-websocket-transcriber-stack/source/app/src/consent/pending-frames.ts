/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Consent frames that arrive while the START before them is still being
 * handled.
 *
 * A client sends CONSENT_EVIDENCE (and may send CONSENT_OBJECTION) straight
 * after START. The socket's message handler does not wait for one frame before
 * handling the next, and START reads the settings and writes the meeting's
 * start event before it registers the meeting on the connection, so a frame
 * right behind it would find no meeting. Frames for a connection whose START
 * is in progress are held here and handed back, in order, once it has
 * registered the meeting. A bounded number is held per connection.
 */
export type HoldResult = 'held' | 'full' | 'no-start';

export class PendingConsentFrames<K> {
    private readonly pending = new Map<K, Array<Record<string, unknown>>>();

    private readonly limit: number;

    constructor(limit = 8) {
        this.limit = limit;
    }

    /** A START began on this connection: hold its consent frames until it has finished. */
    begin(key: K): void {
        this.pending.set(key, []);
    }

    /** Hold a frame for a START in progress on this connection. */
    hold(key: K, frame: Record<string, unknown>): HoldResult {
        const frames = this.pending.get(key);
        if (!frames) {
            return 'no-start';
        }
        if (frames.length >= this.limit) {
            return 'full';
        }
        frames.push(frame);
        return 'held';
    }

    /** The START finished: the frames held for it, in the order they arrived. */
    release(key: K): Array<Record<string, unknown>> {
        const frames = this.pending.get(key) ?? [];
        this.pending.delete(key);
        return frames;
    }

    /** The START was refused or the connection closed: forget its frames. */
    drop(key: K): void {
        this.pending.delete(key);
    }
}
