/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How a meeting was captured and transcribed, as stored on its meeting record
 * (Call.CaptureSource, TranscriptionEngine, TranscriptionLanguage and
 * SpeakerSeparation in the AppSync schema). The START event carries all four;
 * an UPDATE_CAPTURE_CONTEXT event carries the transcription fields again when
 * the engine that transcribes the meeting is not the one START named, as when
 * the MicroVM could not start and Amazon Transcribe took over. See
 * docs/websocket-streaming-api.md#capture-context.
 *
 * Kept free of AWS clients so it can be tested directly.
 */
import type { AsrEngineName } from './asr-microvm';
import type { DiarizationSettings } from './eventtypes';
import type { TranscriptionSettings } from './session-settings';

/**
 * The capture sources a streaming client can name with `clientType`. The
 * Virtual Participant and uploads write their own START events, as
 * VIRTUAL_PARTICIPANT and UPLOAD; any other client is API.
 */
export type StreamingCaptureSource = 'STREAM_AUDIO' | 'BROWSER_EXTENSION' | 'DESKTOP_APP' | 'API';
export type TranscriptionEngineName = 'TRANSCRIBE' | 'MICROVM_ASR';
export type SpeakerSeparation = 'BOTH' | 'REMOTE' | 'LOCAL' | 'NONE';

export type TranscriptionContext = {
    TranscriptionEngine: TranscriptionEngineName,
    /** Amazon Transcribe's language setting; absent for the MicroVM, whose model sets it. */
    TranscriptionLanguage?: string,
    SpeakerSeparation: SpeakerSeparation,
};

export type CaptureContext = TranscriptionContext & {
    CaptureSource: StreamingCaptureSource,
};

const STREAMING_CLIENT_TYPES: ReadonlySet<string> = new Set(['STREAM_AUDIO', 'BROWSER_EXTENSION', 'DESKTOP_APP']);

/**
 * The capture source a START's `clientType` names. Absent, or a value this
 * server does not know, is API: the client is one of the streaming API's own.
 */
export const captureSourceFor = (clientType: unknown): StreamingCaptureSource => {
    if (typeof clientType !== 'string') {
        return 'API';
    }
    const named = clientType.trim().toUpperCase();
    return STREAMING_CLIENT_TYPES.has(named) ? (named as StreamingCaptureSource) : 'API';
};

export const transcriptionEngineName = (engine: AsrEngineName): TranscriptionEngineName =>
    engine === 'microvm' ? 'MICROVM_ASR' : 'TRANSCRIBE';

/** Which channels speaker separation was asked for: ch_0 is REMOTE, ch_1 LOCAL. */
export const speakerSeparationFor = (settings: DiarizationSettings): SpeakerSeparation => {
    const remote = settings.diarizeSystemChannel === true;
    const local = settings.diarizeMicChannel === true;
    if (remote && local) {
        return 'BOTH';
    }
    if (remote) {
        return 'REMOTE';
    }
    return local ? 'LOCAL' : 'NONE';
};

/**
 * The transcription fields of the meeting record for one engine. `diarization`
 * is the meeting's effective per-channel setting (diarizationSettingsFor), which
 * both engines honour; `transcription` the meeting's Amazon Transcribe options.
 */
export const transcriptionContextFor = (
    engine: AsrEngineName,
    diarization: DiarizationSettings,
    transcription: TranscriptionSettings | undefined
): TranscriptionContext => ({
    TranscriptionEngine: transcriptionEngineName(engine),
    ...(engine === 'transcribe' && transcription?.languageCode
        ? { TranscriptionLanguage: transcription.languageCode }
        : {}),
    SpeakerSeparation: speakerSeparationFor(diarization),
});
