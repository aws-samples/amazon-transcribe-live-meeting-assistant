/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Listening for spoken consent replies while the meeting consent flow withholds
 * capture (docs/meeting-consent.md, "Spoken replies").
 *
 * While consent is awaited nothing is captured: no transcript, no recording, no
 * voice assistant. To hear a reply said aloud, the meeting audio still has to
 * reach a speech engine, so this streams it, on a session of its own, to the
 * meeting's transcription engine (Amazon Transcribe or the on-demand MicroVM
 * engine, chosen as transcription chooses it), and hands each final result to
 * the consent flow (spoken-replies.ts) with the name of the participant the
 * platform showed speaking. Nothing else is done with what it hears:
 *
 * - It is a separate session from the transcription service's, with its own
 *   capture of the meeting audio (meeting_audio.monitor, which never carries the
 *   voice assistant's voice). Nothing it hears is written to Kinesis, the
 *   transcript, the recording or the logs, and its text is not kept once the
 *   consent flow has read it.
 * - Partial results are never read.
 * - When consent is granted the session is closed, and transcription starts on
 *   sessions of its own, so no audio heard while consent was awaited reaches
 *   the transcript.
 *
 * The engines are injected (scribe.ts supplies the real ones), so the tests can
 * drive it with fakes.
 */
import type { AsrEngineName } from './asr-microvm-client.js';
import { onParticipantSeen } from './consent-roster.js';
import { RosterTimeline } from './roster-timeline.js';
import { noteSpokenUtterance, SpokenUtterance } from './spoken-replies.js';
import { createLogger } from './logger.js';

const log = createLogger('consent-listener');

/** Mono 16 kHz PCM16, the format both engines are sent. */
const BYTES_PER_SECOND = 16000 * 2;

/** One final result, timed on the session's own audio clock (seconds of audio sent to it). */
export interface HeardResult {
    text: string;
    startSec: number;
    endSec: number;
}

/** One open engine session. */
export interface ListeningSession {
    /** Sends mono 16 kHz PCM16 of the meeting audio. */
    push(pcm: Buffer): void;
    /** Ends the session; resolves once it has stopped and will report nothing more. */
    close(): Promise<void>;
}

export interface ListenerEngines {
    /** The engine this meeting is transcribed with, chosen as transcription chooses it. */
    resolveEngine(): Promise<AsrEngineName>;
    /**
     * Opens an Amazon Transcribe session. `onFinal` gets each final result;
     * `onEnded` is called once if the session ends on its own (closed by the
     * service, or failed). Null when it could not be opened.
     */
    openTranscribe(onFinal: (result: HeardResult) => void, onEnded: () => void): Promise<ListeningSession | null>;
    /** Opens a MicroVM engine session; null when none is available, and Amazon Transcribe is used. */
    openMicrovm(onFinal: (result: HeardResult) => void, onEnded: () => void): Promise<ListeningSession | null>;
    /** Starts capturing the meeting audio; answers a function that stops it. */
    captureMeetingAudio(onPcm: (pcm: Buffer) => void): () => void;
}

export interface ConsentListenerOptions {
    engines: ListenerEngines;
    /** The VP's own names, never attributed a reply. */
    ownIdentities: string[];
    /** How late the platform reports a change of active speaker (scribe.ts rosterLagSeconds). */
    lagSeconds: () => number;
    /** The participant shown speaking when listening starts, or "none". */
    currentSpeaker?: () => string;
    /** Where heard utterances go; the consent flow's registry by default. */
    onHeard?: (utterance: SpokenUtterance) => void;
    /** Wait before reopening a session that ended on its own. */
    retryMs?: number;
    /** Sessions that end within this long of opening count as failures. */
    healthySessionMs?: number;
    /** Consecutive failures after which listening stops until it is asked for again. */
    maxFailures?: number;
    clock?: () => number;
    sleep?: (ms: number) => Promise<void>;
}

export class ConsentListener {
    private readonly options: ConsentListenerOptions;

    private readonly onHeard: (utterance: SpokenUtterance) => void;

    private readonly clock: () => number;

    private readonly sleep: (ms: number) => Promise<void>;

    private wanted = false;

    private session: ListeningSession | null = null;

    private stopCapture: (() => void) | null = null;

    /** Ends the wait for the open session, when it is closed from here. */
    private endWait: (() => void) | null = null;

    private stopSightings: (() => void) | null = null;

    private timeline: RosterTimeline | null = null;

    private bytesSent = 0;

    /** Bumped on every start and stop, so a session opened for an older request is closed. */
    private generation = 0;

    private running: Promise<void> = Promise.resolve();

    /** The engine the current session runs on, for tests and logs. */
    engine: AsrEngineName | null = null;

    constructor(options: ConsentListenerOptions) {
        this.options = options;
        this.onHeard = options.onHeard ?? noteSpokenUtterance;
        this.clock = options.clock ?? Date.now;
        this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    }

    /** Whether listening has been asked for and not stopped. */
    get listening(): boolean {
        return this.wanted;
    }

    /** Starts listening, if it is not already. */
    start(): void {
        if (this.wanted) return;
        this.wanted = true;
        this.generation += 1;
        const generation = this.generation;
        // Who is speaking, on this session's own audio clock.
        this.stopSightings = onParticipantSeen((name) => this.timeline?.record(this.clockSeconds(), name));
        const previous = this.running;
        this.running = previous.then(() => this.run(generation));
    }

    /** Stops listening; resolves once the session is closed and will report nothing more. */
    async stop(): Promise<void> {
        if (this.wanted) {
            this.wanted = false;
            this.generation += 1;
            this.stopSightings?.();
            this.stopSightings = null;
            await this.closeSession();
        }
        await this.running;
    }

    private clockSeconds(): number {
        return this.bytesSent / BYTES_PER_SECOND;
    }

    private current(generation: number): boolean {
        return this.wanted && this.generation === generation;
    }

    private async run(generation: number): Promise<void> {
        let failures = 0;
        const maxFailures = this.options.maxFailures ?? 5;
        while (this.current(generation)) {
            const openedAt = this.clock();
            const ended = await this.openOnce(generation);
            if (!this.current(generation)) return;
            if (ended === 'not-opened' || this.clock() - openedAt < (this.options.healthySessionMs ?? 10_000)) {
                failures += 1;
            } else {
                failures = 0;
            }
            if (failures >= maxFailures) {
                log.error(`[consent] stopped listening for spoken replies after ${failures} failed sessions; chat replies are still read`);
                this.wanted = false;
                this.stopSightings?.();
                this.stopSightings = null;
                return;
            }
            await this.sleep(this.options.retryMs ?? 5000);
        }
    }

    /** Opens one session and waits for it to end; answers why it ended. */
    private async openOnce(generation: number): Promise<'ended' | 'not-opened'> {
        let resolveEnded: () => void = () => undefined;
        const endedOnItsOwn = new Promise<void>((resolve) => {
            resolveEnded = resolve;
        });
        let session: ListeningSession | null = null;
        const onFinal = (result: HeardResult): void => {
            // Only the session that is open now, and only while listening is wanted.
            if (!this.current(generation) || this.session !== session) return;
            this.heard(result);
        };
        const onEnded = (): void => resolveEnded();
        this.endWait = resolveEnded;
        try {
            const engine = await this.options.engines.resolveEngine();
            if (!this.current(generation)) return 'ended';
            if (engine === 'microvm') {
                session = await this.options.engines.openMicrovm(onFinal, onEnded);
                if (session) this.engine = 'microvm';
                else log.warn('[consent] no MicroVM engine session for spoken replies; listening with Amazon Transcribe');
            }
            if (!session && this.current(generation)) {
                session = await this.options.engines.openTranscribe(onFinal, onEnded);
                if (session) this.engine = 'transcribe';
            }
        } catch (error) {
            log.warn(`[consent] could not open a session to listen for spoken replies: ${String(error)}`);
            session = null;
        }
        if (!session) return 'not-opened';
        if (!this.current(generation)) {
            await session.close().catch(() => undefined);
            return 'ended';
        }
        this.session = session;
        this.bytesSent = 0;
        this.timeline = new RosterTimeline(this.options.ownIdentities);
        const speaking = this.options.currentSpeaker?.();
        if (speaking) this.timeline.record(0, speaking);
        const open = session;
        this.stopCapture = this.options.engines.captureMeetingAudio((pcm) => {
            if (this.session !== open) return;
            this.bytesSent += pcm.length;
            open.push(pcm);
        });
        log.info(`[consent] listening for spoken replies (${this.engine === 'microvm' ? 'MicroVM engine' : 'Amazon Transcribe'})`);
        await endedOnItsOwn;
        if (this.session === open) {
            log.info('[consent] the session listening for spoken replies ended; reopening it');
            await this.closeSession();
        }
        return 'ended';
    }

    /** Closes the open session, if any. Its capture stops first, so nothing more is sent to it. */
    private async closeSession(): Promise<void> {
        const session = this.session;
        this.session = null;
        this.endWait?.();
        this.endWait = null;
        this.stopCapture?.();
        this.stopCapture = null;
        this.timeline = null;
        if (session) {
            try {
                await session.close();
            } catch (error) {
                log.warn(`[consent] could not close the session listening for spoken replies: ${String(error)}`);
            }
            log.info('[consent] stopped listening for spoken replies');
        }
    }

    private heard(result: HeardResult): void {
        const text = (result.text || '').trim();
        if (!text) return;
        const timeline = this.timeline;
        const name = timeline && !timeline.isEmpty ? timeline.speakerFor(result.startSec, result.endSec, this.options.lagSeconds()) : null;
        // Handed on and not kept: the consent flow reads it as a reply or drops it.
        this.onHeard({ speaker: name && name !== 'Unknown' ? name : null, text });
    }
}
