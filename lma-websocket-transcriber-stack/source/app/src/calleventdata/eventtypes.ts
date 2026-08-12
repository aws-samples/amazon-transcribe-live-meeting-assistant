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
import type { AsrSessionSet } from './asr-microvm';

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

export type CallMetaData = {
    callId: Uuid,
    fromNumber?: string,
    toNumber?: string,
    shouldRecordCall?: boolean,
    agentId?: string,
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
    /** Label the transcript per voice instead of per audio channel. */
    enableDiarization?: boolean,
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
    accessToken?: string,
    idToken?: string,
    refreshToken?: string,
};

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