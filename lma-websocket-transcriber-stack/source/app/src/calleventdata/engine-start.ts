/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Starting a meeting's transcription engine.
 *
 * The ASR MicroVM engine has to acquire a MicroVM before it can transcribe
 * anything, which takes seconds (about 20 on a cold launch). Audio that arrives
 * meanwhile is held twice: by the MicroVM's channel sessions, which send it
 * when they connect, and in the meeting's audio stream, which Amazon Transcribe
 * reads if the MicroVM cannot be started. The stream is only drained once the
 * MicroVM engine is running.
 *
 * A meeting can end inside that window -- a short stream, or a client that
 * hangs up early. Its end (session-lifecycle.ts) waits for the promise this
 * returns, which settles only once the held audio has gone to an engine: the
 * MicroVM, which startMicrovmAsr then finishes at once, or, when the MicroVM
 * cannot be started, one Amazon Transcribe request for the held audio alone.
 */

import { SocketCallData } from './eventtypes';
import type { AsrEngineName } from './asr-microvm';
import type { TranscribeStartOptions } from './transcribe';

export interface EngineStartDeps {
    /** The engine this meeting asks for, with any reason it cannot have it logged. */
    chooseEngine: (socketData: SocketCallData) => Promise<AsrEngineName>;
    /** Acquire a MicroVM and start its sessions; false when that failed. */
    startMicrovm: (socketData: SocketCallData) => Promise<boolean>;
    startTranscribe: (socketData: SocketCallData, options?: TranscribeStartOptions) => Promise<void>;
    fallbackToTranscribe: () => boolean;
    /** The engine now transcribing the meeting, for its record. */
    noteEngine: (socketData: SocketCallData, engine: AsrEngineName) => void;
    log: {
        warn(message: string): void;
        error(message: string): void;
    };
}

const run = async (socketData: SocketCallData, deps: EngineStartDeps): Promise<void> => {
    const callId = socketData.callMetadata.callId;
    if ((await deps.chooseEngine(socketData)) === 'microvm') {
        if (await deps.startMicrovm(socketData)) {
            deps.noteEngine(socketData, 'microvm');
            // startTranscribe is the ONLY consumer of audioInputStream, and it is
            // not running on this path -- so every frame written to it would sit
            // in the stream's buffer for the whole meeting. At the 16 kHz stereo
            // this engine negotiates that is 64 kB/s, about 230 MB an hour, in a
            // 1 GiB task shared by every other meeting on it. Draining discards
            // each frame as it arrives; writes stay unconditional so the
            // recording is untouched, and this happens only AFTER a successful
            // start so the audio buffered during MicroVM acquisition is still
            // there for the fallback below.
            socketData.audioInputStream?.resume();
            return;
        }
        if (!deps.fallbackToTranscribe()) {
            deps.log.error(
                `[TRANSCRIBING]: [${callId}] - MicroVM ASR could not start and fallback is disabled; this meeting will not be transcribed.`
            );
            return;
        }
        deps.log.warn(`[TRANSCRIBING]: [${callId}] - MicroVM ASR could not start; falling back to Amazon Transcribe.`);
    }
    deps.noteEngine(socketData, 'transcribe');
    if (socketData.ended) {
        // Awaited: the meeting's end waits for this, so the held audio's rows
        // are written before its END event and its stream is not released
        // while the request still reads it.
        await deps.startTranscribe(socketData, { heldAudioOnly: true });
        return;
    }
    // A live meeting's Transcribe loop runs for the rest of the meeting; the
    // stream still holds everything received since the engine was chosen, so
    // the first request starts with it.
    void deps.startTranscribe(socketData);
};

/**
 * Start the meeting's engine, recording the attempt on the meeting
 * (engineStarting) so that its end can wait for it.
 */
export const startTranscriptionEngine = (socketData: SocketCallData, deps: EngineStartDeps): Promise<void> => {
    const starting = run(socketData, deps);
    socketData.engineStarting = starting.catch(() => undefined);
    return starting;
};
