/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which audio connection carries which meeting, where its audio goes while
 * the START that opens a session is still being handled, and how a meeting's
 * session ends: when its client sends END, when its connection closes and nobody
 * reconnects in time, when its client reconnects on a new connection, and when
 * the task shuts down.
 *
 * The server (index.ts) owns the connections and hands this module the effects
 * it cannot perform itself (writing the END event, uploading the recording,
 * stopping the engine), so the order of those effects and the bookkeeping
 * around them can be tested with fake sockets.
 */
import { randomUUID } from 'crypto';

import { pcmSeconds } from '../metering';
import { runWithLogContext } from '../utils/log-context';
import { DetachedSessions } from './detached-sessions';
import { CallMetaData, SocketCallData } from './eventtypes';
import { PendingAudioFrames, pendingAudioLimitBytes } from './pending-audio';

/** How long a meeting waits for its client to reconnect, unless the task's environment says otherwise. */
export const DEFAULT_AUDIO_RECONNECT_GRACE_MS = 60_000;

/**
 * The reconnect window from the task's `AUDIO_RECONNECT_GRACE_MS`. A value that
 * is not a number falls back to the default; 0 or less turns holding off.
 */
export const parseReconnectGraceMs = (raw: string | undefined): number => {
    if (raw === undefined || raw.trim() === '') {
        return DEFAULT_AUDIO_RECONNECT_GRACE_MS;
    }
    const value = Number(raw);
    return Number.isFinite(value) ? value : DEFAULT_AUDIO_RECONNECT_GRACE_MS;
};

// The recording is always two channels of 16-bit PCM (see utils/wav.ts).
const RECORDING_CHANNELS = 2;

/**
 * Where the meeting's timeline is now: the duration of the audio this session
 * has received. The recording holds exactly this audio, and both transcription
 * engines place their transcript times by the audio they have taken from it,
 * so time during which no audio arrived (a held session, a dropped connection
 * that was taken over) does not move it.
 */
export const recordedAudioSeconds = (session: SocketCallData): number =>
    pcmSeconds(session.recordingFileSize ?? 0, session.callMetadata.samplingRate, RECORDING_CHANNELS);

/** What this module needs of a connection. */
export interface SessionSocket {
    close(code?: number, reason?: string): void;
}

export interface SessionLog {
    info(message: string): void;
    warn(message: string): void;
    error(message: string): void;
    debug(message: string): void;
}

export interface SessionLifecycleDeps {
    graceMs: number;
    log: SessionLog;
    isShuttingDown: () => boolean;
    /** Write the meeting's END event. */
    writeCallEndEvent: (callMetaData: CallMetaData) => Promise<void>;
    /** Build the session's recording and upload it (or discard it); runs after the END event. */
    finishRecording: (session: SocketCallData, callMetaData: CallMetaData) => Promise<void>;
    /** Stop the session's transcription engine beyond closing its audio stream (the MicroVM engine). */
    stopEngine: (session: SocketCallData) => Promise<void>;
    /** Start the session's transcription engine. */
    startTranscription: (session: SocketCallData) => Promise<void>;
    /** One frame of the session's audio: to its recording and its transcription engine. */
    writeAudio: (session: SocketCallData, frame: Uint8Array) => void;
    noteSessionEnded: (callMetaData: CallMetaData) => void;
    describe: (callMetaData: CallMetaData) => string;
    describeError: (error: unknown) => string;
}

/** The tokens of the connection a client reconnected with. */
export type ResumeTokens = Pick<CallMetaData, 'accessToken' | 'idToken' | 'refreshToken'>;

export class SessionLifecycle<W extends SessionSocket> {
    private readonly sockets = new Map<W, SocketCallData>();
    // The connection a meeting's session is streaming on now. Changes when a
    // client reconnects and resumes the session, so whatever the session sends
    // the client (consent frames) goes to the connection that is open now.
    private readonly connections = new WeakMap<SocketCallData, { ws: W }>();
    // Sessions whose connection closed without an END, waiting for their client.
    private readonly detached: DetachedSessions<SocketCallData>;
    // Audio for connections whose START is still being handled (pending-audio.ts).
    private readonly pendingAudio = new PendingAudioFrames<W>();

    constructor(private readonly deps: SessionLifecycleDeps) {
        this.detached = new DetachedSessions<SocketCallData>(deps.graceMs, (session) => this.onExpire(session));
    }

    /**
     * A new session on its connection. The returned holder's `ws` follows the
     * session across reconnects.
     */
    register(ws: W, session: SocketCallData): { ws: W } {
        const connection = { ws };
        this.connections.set(session, connection);
        this.sockets.set(ws, session);
        return connection;
    }

    /**
     * A START began on this connection: its audio is held from here until the
     * START has a session for it (`releaseAudio`) or is refused (`dropAudio`).
     * Called before the START handler's first await, so no frame sent behind
     * the START can reach the connection's previous session, if it has one,
     * or find none.
     */
    holdAudio(ws: W, samplingRate: number | undefined): void {
        this.pendingAudio.begin(ws, pendingAudioLimitBytes(samplingRate));
    }

    /**
     * A frame of audio on a connection: held while its START is in progress,
     * otherwise written to the session the connection carries. False when
     * there is neither, and the frame is dropped.
     */
    onAudio(ws: W, frame: Uint8Array): boolean {
        if (this.pendingAudio.hold(ws, frame)) {
            return true;
        }
        const session = this.sockets.get(ws);
        if (!session) {
            return false;
        }
        this.deps.writeAudio(session, frame);
        return true;
    }

    /**
     * The START on this connection has its session: the audio held for it is
     * written to the session in the order it arrived, and later frames go
     * straight to it. Nothing awaits between the two, so a later frame cannot
     * overtake a held one. A START whose session has ended or gone meanwhile
     * (its consent check discarded it) has its audio discarded.
     */
    releaseAudio(ws: W): void {
        const held = this.pendingAudio.release(ws);
        const session = this.sockets.get(ws);
        if (!session || session.ended) {
            if (held.frames.length > 0) {
                this.deps.log.debug(`[START]: Discarding ${held.bytes} bytes of audio held for a START with no session.`);
            }
            return;
        }
        if (held.droppedBytes > 0) {
            this.deps.log.warn(
                `[START]: [${session.callMetadata.callId}] - ${held.droppedFrames} audio frame(s) (${held.droppedBytes} bytes) arrived while START was handled and did not fit in the hold; the oldest were dropped.`
            );
        }
        for (const frame of held.frames) {
            this.deps.writeAudio(session, frame);
        }
    }

    /** The START on this connection was refused or failed: the audio held for it is discarded. */
    dropAudio(ws: W): void {
        const bytes = this.pendingAudio.drop(ws);
        if (bytes > 0) {
            this.deps.log.debug(`[START]: Discarding ${bytes} bytes of audio held for a START that did not start a session.`);
        }
    }

    /** Connections whose START is holding audio now. */
    get pendingAudioCount(): number {
        return this.pendingAudio.size;
    }

    /** The session a connection carries, if any. */
    sessionOf(ws: W): SocketCallData | undefined {
        return this.sockets.get(ws);
    }

    /** Every session on a connection now (not the held ones). */
    connectedSessions(): SocketCallData[] {
        return [...this.sockets.values()];
    }

    /** The meeting's live session: on a connection, or held for its client. */
    findLive(callId: string): SocketCallData | undefined {
        for (const session of this.sockets.values()) {
            if (!session.ended && session.callMetadata?.callId === callId) {
                return session;
            }
        }
        // A meeting waiting for its audio connection to come back is still live:
        // its video stream may reconnect first.
        return this.detached.find(callId);
    }

    /** Drop a session that is ended without an END event (consent discard). */
    discard(ws: W, session: SocketCallData): void {
        this.detached.forget(session);
        if (this.sockets.get(ws) === session) {
            this.sockets.delete(ws);
        }
    }

    /**
     * Continue a meeting's session on the connection its client reconnected
     * with, when the same verified user has one: held after its connection
     * closed, or still on an older connection that has not been seen to close
     * (a dropped network is noticed by the client first). The session keeps
     * its recording file, its transcription engine and its transcript clock;
     * no second START or END is written for the meeting. The audio the client
     * could not send meanwhile is missing from the recording and the transcript
     * alike, so the two stay in step.
     *
     * `beforeTranscription` runs once the session is on the new connection,
     * before a session that had not started transcribing yet is started. The
     * audio held while the START was handled is written to the session after
     * it, and before the engine is started.
     * Undefined when there is nothing to resume: the caller starts a new session.
     */
    async resume(
        ws: W,
        callId: string,
        callerSub: string | undefined,
        tokens: ResumeTokens,
        beforeTranscription?: () => Promise<void>
    ): Promise<{ session: SocketCallData; detachedForMs: number } | undefined> {
        const resumed = this.detached.resume(callId, callerSub) ?? this.takeOver(callId, callerSub, ws);
        if (!resumed) {
            return undefined;
        }
        const { session, detachedForMs } = resumed;
        // The new connection's tokens: the events written from here on carry them.
        session.callMetadata.accessToken = tokens.accessToken;
        session.callMetadata.idToken = tokens.idToken;
        session.callMetadata.refreshToken = tokens.refreshToken;
        const connection = this.connections.get(session);
        if (connection) {
            connection.ws = ws;
        } else {
            this.connections.set(session, { ws });
        }
        this.sockets.set(ws, session);
        this.deps.log.info(
            `[START]: [${callId}] - The client reconnected after ${detachedForMs} ms; continuing the meeting's session (${session.recordingFileSize ?? 0} bytes recorded so far).`
        );
        await beforeTranscription?.();
        this.releaseAudio(ws);
        if (!session.ended && !session.transcriptionStarted && (!session.consent || session.consent.gateOpen)) {
            session.transcriptionStarted = true;
            await this.deps.startTranscription(session);
        }
        return resumed;
    }

    /**
     * A connection closed. Unless the meeting is over or the task is shutting
     * down, its session is held for the client to reconnect to; otherwise the
     * meeting ends now.
     */
    async onConnectionClosed(ws: W): Promise<void> {
        // A START still in progress on it will find its audio gone.
        this.dropAudio(ws);
        const session = this.sockets.get(ws);
        if (!session) {
            return;
        }
        if (!this.deps.isShuttingDown() && !session.ended && this.detached.detach(session)) {
            this.sockets.delete(ws);
            this.deps.log.info(
                `[ON WSCLOSE]: [${session.callMetadata.callId}] - Connection closed without END; holding the meeting for ${this.deps.graceMs} ms for the client to reconnect.`
            );
            return;
        }
        this.deps.log.debug(
            `[ON WSCLOSE]: [${session.callMetadata.callId}] - Writing call end event due to websocket close event ${this.deps.describe(session.callMetadata)}`
        );
        await this.end(ws, session);
    }

    /**
     * End a meeting's session once: its END event, its recording, its engine,
     * and its connection entries. `ws` is the connection the END arrived on, if
     * it arrived on one; the session's current connection is cleared as well.
     */
    async end(ws: W | undefined, session: SocketCallData, callMetaData: CallMetaData = session.callMetadata): Promise<void> {
        if (session.ended) {
            this.deps.log.error(
                `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Duplicate End call event. Already received the end call event: ${this.deps.describe(callMetaData)}`
            );
            return;
        }
        session.ended = true;
        // Ended some other way (consent) while it waited for its client.
        this.detached.forget(session);
        // Inside the ran-once guard, so a second close does not count the same
        // meeting twice.
        this.deps.noteSessionEnded(session.callMetadata);
        try {
            await this.deps.writeCallEndEvent(callMetaData);
            await this.deps.finishRecording(session, callMetaData);
        } finally {
            if (session.audioInputStream) {
                session.audioInputStream.end();
                session.audioInputStream.destroy();
            }
            // Flushes the tail utterance on each channel, then terminates the
            // MicroVM. No-op for meetings transcribed by Amazon Transcribe.
            try {
                await this.deps.stopEngine(session);
            } catch (error) {
                this.deps.log.error(
                    `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Error stopping the MicroVM ASR engine: ${this.deps.describeError(error)}`
                );
            }
            // Only this meeting's entry: a client that reuses the connection
            // may have started its next meeting while this one finished.
            if (ws && this.sockets.get(ws) === session) {
                this.sockets.delete(ws);
            }
            // And wherever else it streams now, after a reconnect.
            const current = this.connections.get(session)?.ws;
            if (current && this.sockets.get(current) === session) {
                this.sockets.delete(current);
            }
        }
    }

    /** End every held meeting: on shutdown, nobody reconnects to this task. */
    async endHeldSessions(): Promise<void> {
        for (const session of this.detached.drain()) {
            try {
                await this.end(undefined, session);
            } catch (error) {
                this.deps.log.error(
                    `[SHUTDOWN]: Error ending ${session.callMetadata.callId}: ${this.deps.describeError(error)}`
                );
            }
        }
    }

    get heldCount(): number {
        return this.detached.size;
    }

    /**
     * The meeting's live session on another connection of the same verified
     * user, moved off that connection, which is closed: the client has
     * reconnected and the new connection carries the meeting from now on.
     * `detachedForMs` is 0 because when the old connection stopped carrying
     * audio is not known here; nothing that times the meeting reads it.
     */
    private takeOver(
        callId: string,
        callerSub: string | undefined,
        ws: W
    ): { session: SocketCallData; detachedForMs: number } | undefined {
        if (!callerSub) {
            return undefined;
        }
        for (const [other, session] of this.sockets) {
            if (other !== ws && !session.ended && session.callMetadata.callId === callId && session.ownerSub === callerSub) {
                this.sockets.delete(other);
                try {
                    other.close(1000, 'the meeting continues on a newer connection');
                } catch {
                    // already closing
                }
                return { session, detachedForMs: 0 };
            }
        }
        return undefined;
    }

    private onExpire(session: SocketCallData): void {
        runWithLogContext({ sessionId: randomUUID(), callId: session.callMetadata.callId }, () => {
            this.deps.log.info(
                `[ON WSCLOSE]: [${session.callMetadata.callId}] - The client did not reconnect within ${this.deps.graceMs} ms; ending the meeting.`
            );
            this.end(undefined, session).catch((error) => {
                this.deps.log.error(
                    `[ON WSCLOSE]: [${session.callMetadata.callId}] - Error ending the meeting: ${this.deps.describeError(error)}`
                );
            });
        });
    }
}
