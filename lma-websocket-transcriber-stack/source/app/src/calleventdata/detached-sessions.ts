/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Meetings whose audio connection closed without an END, held for a short
 * while so the client can reconnect and carry on with the same meeting.
 *
 * The desktop capture apps reconnect after a dropped connection by opening a
 * new one and sending START again with the same callId. Ending the meeting as
 * soon as the first connection closed, and starting it afresh on the second,
 * split one meeting into two sessions that each wrote the meeting's recording
 * under the same name, so the later one replaced the earlier one, and that each
 * started the transcript's clock at zero. Holding the session instead lets the
 * reconnected client continue it: the same recording file, the same
 * transcription engine and its clock, one END when the meeting really ends.
 *
 * A session is resumed only by a START for the same callId from the same
 * verified Cognito user that opened it.
 */

export interface DetachableSession {
    callMetadata: { callId: string };
    ownerSub?: string;
    ended: boolean;
}

interface Held<T> {
    session: T;
    timer: NodeJS.Timeout;
    detachedAtMs: number;
}

export class DetachedSessions<T extends DetachableSession> {
    private readonly held = new Map<string, Held<T>>();

    /**
     * @param graceMs how long a session waits for its client; 0 ends it at once.
     * @param onExpire ends a session nobody resumed in time.
     */
    constructor(
        private readonly graceMs: number,
        private readonly onExpire: (session: T) => void
    ) {}

    /**
     * Hold a session whose connection closed. False when it cannot be held
     * (holding is off, the session has ended or has no verified owner, or
     * another session of the same meeting is already held): the caller ends it
     * now, as before.
     */
    detach(session: T, nowMs: number = Date.now()): boolean {
        const callId = session.callMetadata.callId;
        if (this.graceMs <= 0 || session.ended || !session.ownerSub || this.held.has(callId)) {
            return false;
        }
        const timer = setTimeout(() => {
            if (this.held.get(callId)?.session === session) {
                this.held.delete(callId);
                this.onExpire(session);
            }
        }, this.graceMs);
        // A held session must not keep the process alive on its own.
        timer.unref?.();
        this.held.set(callId, { session, timer, detachedAtMs: nowMs });
        return true;
    }

    /**
     * Take back the held session of a meeting for its owner's new connection.
     * Undefined when there is none, or when the caller is not its owner (the
     * session stays held for its owner).
     */
    resume(callId: string, callerSub: string | undefined, nowMs: number = Date.now()):
        { session: T; detachedForMs: number } | undefined {
        const entry = this.held.get(callId);
        if (!entry || !callerSub || entry.session.ownerSub !== callerSub || entry.session.ended) {
            return undefined;
        }
        clearTimeout(entry.timer);
        this.held.delete(callId);
        return { session: entry.session, detachedForMs: nowMs - entry.detachedAtMs };
    }

    /** The held session of a meeting, if any, without taking it. */
    find(callId: string): T | undefined {
        return this.held.get(callId)?.session;
    }

    /** Stop holding a session that was ended some other way. */
    forget(session: T): void {
        const callId = session.callMetadata.callId;
        const entry = this.held.get(callId);
        if (entry?.session === session) {
            clearTimeout(entry.timer);
            this.held.delete(callId);
        }
    }

    /** Every held session, released from holding: for shutdown, which ends them at once. */
    drain(): T[] {
        const sessions = [...this.held.values()].map((entry) => {
            clearTimeout(entry.timer);
            return entry.session;
        });
        this.held.clear();
        return sessions;
    }

    get size(): number {
        return this.held.size;
    }
}
