/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { TranscriptEvent } from '@aws-sdk/client-transcribe-streaming';
import stream from 'stream';
import { WriteStream } from 'fs';
import type { AsrSessionSet } from './asr-microvm';
import type { SessionSettings } from './session-settings';
import type { ConsentSession } from '../consent/session';
import type { CaptureContext, TranscriptionContext, TranscriptionEngineName } from './capture-context';

export type Uuid = string;             // UUID as defined by RFC#4122

export type EventType =
    | 'START' // required
    | 'ADD_TRANSCRIPT_SEGMENT' // required
    | 'UPDATE_AGENT' // optional
    | 'ADD_S3_RECORDING_URL'  // optional
    | 'ADD_S3_VIDEO_RECORDING_URL' // optional
    | 'ADD_VIDEO_KEYFRAME' // optional
    | 'UPDATE_CAPTURE_CONTEXT' // optional
    | 'END'; // required

export type CallEventBase<Type extends EventType = EventType> = {
    EventType: Type,
    CallId: Uuid,
    CreatedAt?: string,
    UpdatedAt?: string,
};

export type CallStartEvent = CallEventBase<'START'> & Partial<CaptureContext> & {
    CustomerPhoneNumber: string,
    SystemPhoneNumber: string,
    AgentId: string | undefined,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

// The transcription fields of the meeting record again, when the engine that
// transcribes the meeting is not the one its START named (see capture-context.ts).
export type CallCaptureContextEvent = CallEventBase<'UPDATE_CAPTURE_CONTEXT'> & TranscriptionContext & {
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

export type CallEndEvent = CallEventBase<'END'> & {
    CustomerPhoneNumber: string,
    SystemPhoneNumber: string,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

export type CallRecordingEvent = CallEventBase<'ADD_S3_RECORDING_URL'> & {
    RecordingUrl: string,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

// Same shape the Virtual Participant emits, so the existing
// call_event_processor -> updateVideoRecordingUrl pipeline consumes it as-is.
export type CallVideoRecordingEvent = CallEventBase<'ADD_S3_VIDEO_RECORDING_URL'> & {
    VideoRecordingUrl: string,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

// One key frame of the desktop capture app's video (see videokeyframes.ts). The
// Virtual Participant sends the same fields; see docs/meeting-video.md.
export type CallVideoKeyframeEvent = CallEventBase<'ADD_VIDEO_KEYFRAME'> & {
    FrameId: string,
    Timestamp: number,
    VideoOffset: number | null,
    ImageUrl: string,
    ThumbnailUrl: string,
    Width: number,
    Height: number,
    Source: 'DESKTOP_CAPTURE',
    ShareDetected: null,
    ChangeScore: number,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

export type AddTranscriptSegmentEvent = CallEventBase<'ADD_TRANSCRIPT_SEGMENT'> & {
    Channel?: string,
    ParticipantName?: string,
    SegmentId?: string,
    StartTime?: number,
    EndTime?: number,
    Transcript?: string,
    IsPartial?: boolean,
    Sentiment?: string,
    TranscriptEvent?: TranscriptEvent,
    Speaker: string,
    /**
     * The language Amazon Transcribe identified for the result the segment came
     * from (e.g. `es-US`), when language identification is on, or the language
     * the ASR MicroVM engine identified from a final's text (see
     * microvmRowLanguageCode). Absent when neither engine names one: Amazon
     * Transcribe with a fixed language, and MicroVM partials and short rows.
     */
    LanguageCode?: string,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

export interface ChannelSpeakerData {
    currentSpeakerName: string | null;
    speakers: string[];
    startTimes: number[];
}

/**
 * Per-channel Amazon Transcribe speaker partitioning (diarization).
 *
 * Amazon Transcribe's `ShowSpeakerLabel` is a STREAM-level flag and we send one
 * 2-channel stream, so these two booleans do not map 1:1 onto the API: the flag
 * is enabled when EITHER channel opts in, and the resulting labels are applied
 * only to the channel(s) that asked for them.
 *
 * Named by channel ROLE, not by ch_0/ch_1, so clients never need to know the
 * interleave order — the server owns that mapping (see CHANNEL_* below).
 */
export type DiarizationSettings = {
    /** Diarize the system / meeting audio channel (ch_0). */
    diarizeSystemChannel?: boolean,
    /** Diarize the microphone channel (ch_1). */
    diarizeMicChannel?: boolean,
};

/** Transcribe's channel id for the system / meeting audio (shared tab, screen). */
export const CHANNEL_SYSTEM = 'ch_0';
/** Transcribe's channel id for the local microphone. */
export const CHANNEL_MIC = 'ch_1';

export type CallMetaData = {
    callId: Uuid,
    /**
     * The meeting-audio (ch_0) speaker label. A client sends it as
     * `remoteSpeakerName`, or as `fromNumber` if written for LMA before 0.4.0;
     * see speaker-fields.ts.
     */
    fromNumber?: string,
    /** Stored as the meeting's SystemPhoneNumber; nothing else reads it. */
    toNumber?: string,
    shouldRecordCall?: boolean,
    /**
     * The microphone (ch_1) speaker label. A client sends it as
     * `localSpeakerName`, or as `agentId` if written for LMA before 0.4.0.
     */
    agentId?: string,
    /** The documented spelling of `agentId`; copied onto it when the frame is read. */
    localSpeakerName?: string,
    /** The documented spelling of `fromNumber`; copied onto it when the frame is read. */
    remoteSpeakerName?: string,
    samplingRate: number,
    callEvent: string,
    activeSpeaker: string,
    /**
     * Transcription engine for this meeting: 'transcribe' (Amazon Transcribe
     * streaming) or 'microvm' (on-demand ASR + diarization MicroVM). Absent
     * means the deployment default; asking for diarization also selects
     * 'microvm', since that is where speaker labels come from.
     */
    asrEngine?: string,
    /**
     * How many people share this client's audio, when the user knows. Only they
     * can know it — the Upload Audio page asks the same question. Absent or 0
     * means discover as many speakers as appear.
     */
    maxSpeakers?: number,
    // START_VIDEO only: ms between audio-stream start and video-stream start,
    // applied as an offset when muxing so video aligns with audio/transcript.
    videoTimeOffsetMs?: number,
    // START_VIDEO only: true when this is a socket RECONNECT for a video
    // stream whose client-side encoder session never restarted — the bytes
    // continue the same fMP4 stream, so the server appends to the same file
    // instead of rotating to a new segment.
    videoResume?: boolean,
    channels: {
        [channelId: string]: ChannelSpeakerData;
    };
    /**
     * Server-managed (never sent by a client): running count of final results and
     * how many carried speaker labels, so a call where diarization was requested
     * but Transcribe returned nothing can be warned about exactly once.
     */
    diarizationDiagnostics?: {
        finals: number;
        labelled: number;
        warned: boolean;
        /**
         * Highest window count already logged for the in-progress result, per
         * channel, so the info-level diagnostic fires once per window boundary
         * rather than once per partial. Reset when the result changes, so it
         * cannot grow with the length of the meeting.
         */
        windowMarks?: {
            [channelId: string]: {
                resultId: string;
                settledEmitted: number;
                logged: number;
                /** Window anchor, pinned on first sight so it cannot drift. */
                origin?: number;
            };
        };
    };
    /**
     * START only: which client this is -- STREAM_AUDIO, BROWSER_EXTENSION or
     * DESKTOP_APP -- stored on the meeting record as its capture source. Absent
     * or unknown is stored as API. See capture-context.ts.
     */
    clientType?: string,
    /**
     * START only: the client's consent step (see consent/rules.ts and
     * docs/meeting-consent.md). Absent from clients without one.
     */
    consent?: unknown,
    accessToken?: string,
    idToken?: string,
    refreshToken?: string,
} & DiarizationSettings;

export type SocketCallData = {
    callMetadata: CallMetaData,
    audioInputStream?: stream.PassThrough,
    writeRecordingStream?: WriteStream,
    recordingFileSize?: number
    /**
     * MicroVM ASR engine state for this call: the MicroVM lease, the stereo
     * de-interleaver, and one WebSocket session per audio channel. Undefined
     * when the meeting is transcribed by Amazon Transcribe.
     */
    asr?: AsrSessionSet,
    /**
     * When the session was created. Not a position on the meeting's timeline:
     * that is the audio received (session-lifecycle.ts recordedAudioSeconds),
     * which leaves out time the audio connection was down.
     */
    startStreamTime: Date,
    speakerEvents: [],
    ended: boolean,
    /**
     * Cognito `sub` of the user whose verified token opened this call's audio
     * socket. Used to authorize later actions on the same callId (notably
     * START_VIDEO on a SECOND socket) so one user cannot attach media to, or
     * pull audio out of, another user's call. Undefined only for connections
     * that predate the check (defensive; the handler treats that as a denial).
     */
    ownerSub?: string
    /**
     * The meeting's Amazon Transcribe options and recording default, read once
     * from the configuration when it started and kept for its whole life, so
     * every engine path and every reconnect uses the same values.
     */
    sessionSettings?: SessionSettings
    /**
     * The meeting's consent, when its client sent a consent step and the
     * deployment requires one. While its gate is closed no audio is transcribed
     * or recorded.
     */
    consent?: ConsentSession
    /** Whether transcription was started, which an opt-in meeting defers until its consent allows it. */
    transcriptionStarted?: boolean,
    /**
     * This session's local recording files: the raw PCM it writes as audio
     * arrives, and the WAV built from it when the meeting ends. Named per
     * session, so a later session of the same meeting on this task cannot
     * write into a file an earlier one is still uploading.
     */
    recordingTempPath?: string,
    recordingWavPath?: string,
    /**
     * The transcription engine the meeting record names: the one its START event
     * named, or the last UPDATE_CAPTURE_CONTEXT written for it.
     */
    recordedTranscriptionEngine?: TranscriptionEngineName
};