/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { 
    TranscriptEvent,
    UtteranceEvent,
    CategoryEvent,
} from '@aws-sdk/client-transcribe-streaming';
import stream from 'stream';
import { WriteStream } from 'fs';

export type Uuid = string;             // UUID as defined by RFC#4122

export type EventType =
    | 'START' // required
    | 'ADD_TRANSCRIPT_SEGMENT' // required
    | 'UPDATE_AGENT' // optional
    | 'ADD_S3_RECORDING_URL'  // optional
    | 'ADD_S3_VIDEO_RECORDING_URL' // optional
    | 'ADD_CALL_CATEGORY' // optional
    | 'END'; // required

export type CallEventBase<Type extends EventType = EventType> = {
    EventType: Type,
    CallId: Uuid,
    CreatedAt?: string,
    UpdatedAt?: string,
};

export type CallStartEvent = CallEventBase<'START'> & {
    CustomerPhoneNumber: string,
    SystemPhoneNumber: string,
    AgentId: string | undefined,
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
    UtteranceEvent?: UtteranceEvent,
    Speaker: string,
    AccessToken?: string,
    IdToken?: string,
    RefreshToken?: string,
};

export type AddCallCategoryEvent = CallEventBase<'ADD_CALL_CATEGORY'> & {
    CategoryEvent: CategoryEvent,
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
    fromNumber?: string,
    toNumber?: string,
    shouldRecordCall?: boolean,
    agentId?: string,
    samplingRate: number,
    callEvent: string,
    activeSpeaker: string,
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
            [channelId: string]: { resultId: string; settledEmitted: number; logged: number };
        };
    };
    accessToken?: string,
    idToken?: string,
    refreshToken?: string,
} & DiarizationSettings;

export type SocketCallData = {
    callMetadata: CallMetaData,
    audioInputStream?: stream.PassThrough,
    writeRecordingStream?: WriteStream,
    recordingFileSize?: number
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
};