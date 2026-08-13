/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { FastifyInstance } from 'fastify';
import stream from 'stream';

import {
    TranscriptEvent,
    TranscribeStreamingClient,
    StartStreamTranscriptionCommand,
    TranscriptResultStream,
    StartCallAnalyticsStreamTranscriptionCommand,
    StartCallAnalyticsStreamTranscriptionCommandInput,
    CallAnalyticsTranscriptResultStream,
    ConfigurationEvent,
    ParticipantRole,
    ChannelDefinition,
    StartStreamTranscriptionCommandInput,
    ContentRedactionOutput,
    LanguageCode,
    ContentRedactionType,
    Item,
    Result
} from '@aws-sdk/client-transcribe-streaming';

import { KinesisClient, PutRecordCommand } from '@aws-sdk/client-kinesis';

import {
    CallStartEvent,
    CallEndEvent,
    CallRecordingEvent,
    CallVideoRecordingEvent,
    AddTranscriptSegmentEvent,
    SocketCallData,
    CallMetaData,
    ChannelSpeakerData,
    DiarizationSettings,
    CHANNEL_SYSTEM
} from './eventtypes';

import {
    DEFAULT_MIN_RUN_SECONDS,
    DEFAULT_MIN_RUN_WORDS,
    RunThresholds,
    anyChannelDiarized,
    appendSpeakerLabel,
    buildSpeakerRuns,
    describeRuns,
    diarizationEnabledFor,
    formatSpeakerLabel,
    runTranscript,
    smoothSpeakerRuns
} from './diarization';

// Import from the concrete module (not the ../utils barrel): the barrel pulls
// in jwt-verifier, which requires USERPOOL_ID at import time and would break
// offline tests that import this module.
import { normalizeErrorForLogging } from '../utils/common';

const formatPath = function (path: string) {
    let pathOut = path;
    if (path.length > 0 && path.charAt(path.length - 1) != '/') {
        pathOut += '/';
    }
    return pathOut;
};

import dotenv from 'dotenv';
// dotenv v17 prints an "injected env" banner to stdout by default; quiet
// suppresses it to keep production logs clean.
dotenv.config({ quiet: true });

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const TRANSCRIBE_API_MODE = process.env['TRANSCRIBE_API_MODE'] || 'standard';
const isTCAEnabled = TRANSCRIBE_API_MODE === 'analytics';
const TRANSCRIBE_LANGUAGE_CODE =
    process.env['TRANSCRIBE_LANGUAGE_CODE'] || 'en-US';
const TRANSCRIBE_LANGUAGE_OPTIONS =
    process.env['TRANSCRIBE_LANGUAGE_OPTIONS'] || undefined;
const TRANSCRIBE_PREFERRED_LANGUAGE =
    process.env['TRANSCRIBE_PREFERRED_LANGUAGE'] || 'None';
const CUSTOM_VOCABULARY_NAME =
    process.env['CUSTOM_VOCABULARY_NAME'] || undefined;
const CUSTOM_LANGUAGE_MODEL_NAME =
    process.env['CUSTOM_LANGUAGE_MODEL_NAME'] || undefined;
const IS_CONTENT_REDACTION_ENABLED =
    (process.env['IS_CONTENT_REDACTION_ENABLED'] || '') === 'true';
const CONTENT_REDACTION_TYPE = process.env['CONTENT_REDACTION_TYPE'] || 'PII';
const TRANSCRIBE_PII_ENTITY_TYPES =
    process.env['TRANSCRIBE_PII_ENTITY_TYPES'] || undefined;
const TCA_DATA_ACCESS_ROLE_ARN = process.env['TCA_DATA_ACCESS_ROLE_ARN'] || '';
const CALL_ANALYTICS_FILE_PREFIX = formatPath(
    process.env['CALL_ANALYTICS_FILE_PREFIX'] || 'lca-call-analytics-json/'
);
const RECORDINGS_BUCKET_NAME = process.env['RECORDINGS_BUCKET_NAME'] || null;
// optional - disable post call analytics output
const IS_TCA_POST_CALL_ANALYTICS_ENABLED =
    (process.env['IS_TCA_POST_CALL_ANALYTICS_ENABLED'] || 'false') === 'true';
// optional - when redaction is enabled, choose 'redacted' only (dafault), or 'redacted_and_unredacted' for both
const POST_CALL_CONTENT_REDACTION_OUTPUT =
    process.env['POST_CALL_CONTENT_REDACTION_OUTPUT'] || 'redacted';
const kdsStreamName = process.env['KINESIS_STREAM_NAME'] || '';
// Deployment-wide DEFAULT for speaker partitioning (CFN parameter
// ShowSpeakerLabel), used only when the client sends neither
// diarizeSystemChannel nor diarizeMicChannel in its START frame. An explicit
// client flag always wins — see diarizationSettingsFor(). Defaults to false so a
// deployment that never sets it behaves exactly as it did before the feature.
const showSpeakerLabelDefault =
    (process.env['SHOW_SPEAKER_LABEL'] || 'false') === 'true';
// Minimum size for a run of same-speaker words to be split out as its own
// transcript segment. Both must be cleared. Empirically fitted to a real
// two-speaker recording (spurious runs measured 1-2 words / 0.1-0.9s; real turns
// 6-42 words / 1.2-13.4s) — overridable without a code change so the values can
// be re-tuned from the [DIARIZATION] log lines these thresholds produce.
const diarizationRunThresholds: RunThresholds = {
    minWords: parseInt(
        process.env['DIARIZATION_MIN_RUN_WORDS'] || String(DEFAULT_MIN_RUN_WORDS),
        10
    ),
    minSeconds: parseFloat(
        process.env['DIARIZATION_MIN_RUN_SECONDS'] || String(DEFAULT_MIN_RUN_SECONDS)
    ),
};
const DEBUG = (process.env['DEBUG'] || 'false') === 'true';

const tcaOutputLocation = `s3://${RECORDINGS_BUCKET_NAME}/${CALL_ANALYTICS_FILE_PREFIX}`;

type transcriptionCommandInput<TCAEnabled> = TCAEnabled extends true
    ? StartCallAnalyticsStreamTranscriptionCommandInput
    : StartStreamTranscriptionCommandInput;

const kinesisClient = new KinesisClient({ region: AWS_REGION });
const transcribeClient = new TranscribeStreamingClient({ region: AWS_REGION });

/**
 * Effective per-channel diarization settings for a call.
 *
 * Precedence: an EXPLICIT client flag always wins. Only when the client sent
 * neither flag do we fall back to the deployment default (CFN ShowSpeakerLabel),
 * applied to both channels. Kept here rather than in diarization.ts because it
 * reads the environment, and diarization.ts must stay env-free so the offline
 * unit tests can import it.
 */
const diarizationSettingsFor = (callMetaData: CallMetaData): DiarizationSettings => {
    const clientSpecified =
        typeof callMetaData.diarizeSystemChannel === 'boolean' ||
        typeof callMetaData.diarizeMicChannel === 'boolean';
    if (clientSpecified) {
        return {
            diarizeSystemChannel: callMetaData.diarizeSystemChannel === true,
            diarizeMicChannel: callMetaData.diarizeMicChannel === true,
        };
    }
    return {
        diarizeSystemChannel: showSpeakerLabelDefault,
        diarizeMicChannel: showSpeakerLabelDefault,
    };
};

export const writeCallEvent = async (
    callEvent: CallStartEvent | CallEndEvent | CallRecordingEvent | CallVideoRecordingEvent,
    server: FastifyInstance
) => {
    const putParams = {
        StreamName: kdsStreamName,
        PartitionKey: callEvent.CallId,
        Data: Buffer.from(JSON.stringify(callEvent)),
    };

    const putCmd = new PutRecordCommand(putParams);
    try {
        // MUST be awaited: without it the surrounding try/catch cannot catch a
        // rejection (e.g. ProvisionedThroughputExceededException), and Node's
        // default unhandled-rejection behaviour then kills the whole task —
        // dropping every concurrent call.
        await kinesisClient.send(putCmd);
        server.log.debug(
            `[${callEvent.EventType}]: ${callEvent.CallId} - Written ${
                callEvent.EventType
            } Event to KDS: ${JSON.stringify(callEvent)}`
        );
    } catch (error) {
        server.log.debug(
            `[${callEvent.EventType}]: ${callEvent.CallId} - Error writing ${
                callEvent.EventType
            } Call Event to KDS : ${normalizeErrorForLogging(
                error
            )} Event: ${JSON.stringify(callEvent)}`
        );
    }
};

export const writeCallStartEvent = async (
    callMetaData: CallMetaData,
    server: FastifyInstance
): Promise<void> => {
    const callStartEvent: CallStartEvent = {
        EventType: 'START',
        CallId: callMetaData.callId,
        CustomerPhoneNumber: callMetaData.fromNumber || 'Customer Phone',
        SystemPhoneNumber: callMetaData.toNumber || 'System Phone',
        AgentId: callMetaData.agentId,
        CreatedAt: new Date().toISOString(),
        AccessToken: callMetaData.accessToken,
        IdToken: callMetaData.idToken,
        RefreshToken: callMetaData.refreshToken,
    };
    await writeCallEvent(callStartEvent, server);
};

export const writeCallEndEvent = async (
    callMetaData: CallMetaData,
    server: FastifyInstance
): Promise<void> => {
    const callEndEvent: CallEndEvent = {
        EventType: 'END',
        CallId: callMetaData.callId,
        CustomerPhoneNumber: callMetaData.fromNumber || 'Customer Phone',
        SystemPhoneNumber: callMetaData.toNumber || 'System Phone',
        AccessToken: callMetaData.accessToken,
        IdToken: callMetaData.idToken,
        RefreshToken: callMetaData.refreshToken,
    };
    await writeCallEvent(callEndEvent, server);
};

export const writeCallRecordingEvent = async (
    callMetaData: CallMetaData,
    recordingUrl: string,
    server: FastifyInstance
): Promise<void> => {
    const callRecordingEvent: CallRecordingEvent = {
        EventType: 'ADD_S3_RECORDING_URL',
        CallId: callMetaData.callId,
        RecordingUrl: recordingUrl,
        AccessToken: callMetaData.accessToken,
        IdToken: callMetaData.idToken,
        RefreshToken: callMetaData.refreshToken,
    };
    await writeCallEvent(callRecordingEvent, server);
};

export const writeCallVideoRecordingEvent = async (
    callMetaData: CallMetaData,
    videoRecordingUrl: string,
    server: FastifyInstance
): Promise<void> => {
    const callVideoRecordingEvent: CallVideoRecordingEvent = {
        EventType: 'ADD_S3_VIDEO_RECORDING_URL',
        CallId: callMetaData.callId,
        VideoRecordingUrl: videoRecordingUrl,
        AccessToken: callMetaData.accessToken,
        IdToken: callMetaData.idToken,
        RefreshToken: callMetaData.refreshToken,
    };
    await writeCallEvent(callVideoRecordingEvent, server);
};

// True when a Transcribe streaming error indicates the SessionId we tried to
// resume is no longer valid (expired / closed / unknown), so the next attempt
// must start a fresh session rather than reuse the stale id. Kept in sync with
// the equivalent check in the Virtual Participant scribe (scribe.ts).
const isStaleSessionError = (error: unknown): boolean => {
    const err = error as { name?: string; message?: string };
    const message = (err?.message ?? '').toLowerCase();
    // Require the word "session" on every message-based match: otherwise
    // unrelated errors that merely contain "has expired" (e.g. the STS
    // "security token ... has expired" credential error) would be misread as a
    // stale SessionId and needlessly discard a still-valid session.
    return (
        (message.includes('session') &&
            (message.includes('expired') ||
                message.includes('not found') ||
                message.includes('invalid'))) ||
        err?.name === 'SessionExpiredException'
    );
};

export const startTranscribe = async (
    socketCallMap: SocketCallData,
    server: FastifyInstance
) => {
    const callMetaData = socketCallMap.callMetadata;
    const audioInputStream = socketCallMap.audioInputStream;
    // Resolved ONCE per call, not per Transcribe session: the settings come from
    // the START frame and must survive every reconnect unchanged.
    const diarization = diarizationSettingsFor(callMetaData);
    server.log.info(
        `[DIARIZATION]: [${callMetaData.callId}] - Speaker partitioning: system/meeting channel (${CHANNEL_SYSTEM})=${
            diarization.diarizeSystemChannel === true
        }, microphone channel=${diarization.diarizeMicChannel === true}` +
            `, split thresholds: minWords=${diarizationRunThresholds.minWords}` +
            ` minSeconds=${diarizationRunThresholds.minSeconds}`
    );
    // MAX_RETRIES bounds *consecutive* failures, not cumulative ones over the
    // life of the meeting (see HEALTHY_SESSION_MS below). A session that has
    // streamed successfully resets the counter, so a long meeting is not
    // aborted merely because it accumulated a handful of transient
    // reconnects spread across an hour (GitHub #292).
    const MAX_RETRIES = 5;
    // A session that ran at least this long is treated as "healthy" — the next
    // restart starts from retryCount 0. Note: duration only. We deliberately do
    // NOT count "received at least one transcript event" as healthy: a session
    // that Transcribe accepts, emits a single event on, then closes almost
    // immediately would otherwise reset the counter every time and reconnect
    // forever, hammering StartStreamTranscription in a tight loop that never
    // reaches MAX_RETRIES.
    const HEALTHY_SESSION_MS = 10_000;
    // Backoff between consecutive failed restarts (linear, capped).
    const RETRY_BACKOFF_MS = 2_000;
    const MAX_BACKOFF_MS = 10_000;
    // Floor applied to EVERY reconnect (even a healthy retryCount==0 one) so a
    // rapid clean-close/reopen cycle can never become a 0ms hot loop.
    const MIN_BACKOFF_MS = 500;
    let sessionId: string | undefined;

    // Amazon Transcribe streaming timestamps (Item.StartTime / EndTime) are
    // relative to the audio sent in EACH StartStreamTranscription request and
    // reset to 0 on every (re)connect — reusing SessionId does NOT resume the
    // timeline (it is only an identifier). So each time we reconnect we carry a
    // cumulative offset (seconds) equal to the highest segment EndTime seen so
    // far, keeping the meeting transcript timeline monotonic instead of
    // restarting at 0 and overlapping earlier segments (GitHub #292).
    const startTranscribeSession = async (retryCount = 0, timeOffsetSeconds = 0): Promise<void> => {
        // Stop retrying once the call has legitimately ended (socket closed /
        // end event) — otherwise we'd spin forever after a normal hang-up.
        if (socketCallMap.ended) {
            server.log.info(`[TRANSCRIBING]: [${callMetaData.callId}] - Call ended; not (re)starting transcription session.`);
            return;
        }
        if (retryCount >= MAX_RETRIES) {
            server.log.error(`[TRANSCRIBING]: [${callMetaData.callId}] - Max consecutive retries (${MAX_RETRIES}) reached. Aborting transcription.`);
            return;
        }

        const attemptStartMs = Date.now();
        // Highest absolute segment EndTime observed during this attempt; seeds
        // the next reconnect's offset. Starts at the incoming offset so an
        // attempt that produces nothing still carries the timeline forward.
        let observedMaxEndTime = timeOffsetSeconds;
        // Recompute retryCount for the *next* attempt: reset to 0 if this
        // attempt was healthy (ran long enough), otherwise increment.
        // Centralised so both the clean-close and the error paths stay
        // consistent.
        const nextRetryCount = (): number => {
            const healthy = Date.now() - attemptStartMs >= HEALTHY_SESSION_MS;
            return healthy ? 0 : retryCount + 1;
        };
        // Always wait at least MIN_BACKOFF_MS between reconnects — even a
        // healthy (attempt==0) reconnect — so a rapid clean-close/reopen cycle
        // cannot spin into a tight loop.
        const backoff = async (attempt: number): Promise<void> => {
            const delay = Math.max(
                MIN_BACKOFF_MS,
                Math.min(attempt * RETRY_BACKOFF_MS, MAX_BACKOFF_MS)
            );
            await new Promise((resolve) => setTimeout(resolve, delay));
        };

        try {
            server.log.debug(
                `[${callMetaData.callEvent}]: [${
                    callMetaData.callId
                }] - Starting transcribe:  ${JSON.stringify(callMetaData)}`
            );

            const transcribeInput = async function* () {
                if (isTCAEnabled) {
                    const channel0: ChannelDefinition = {
                        ChannelId: 0,
                        ParticipantRole: ParticipantRole.CUSTOMER,
                    };
                    const channel1: ChannelDefinition = {
                        ChannelId: 1,
                        ParticipantRole: ParticipantRole.AGENT,
                    };
                    const channel_definitions: ChannelDefinition[] = [];
                    channel_definitions.push(channel0);
                    channel_definitions.push(channel1);
                    const configuration_event: ConfigurationEvent = {
                        ChannelDefinitions: channel_definitions,
                    };
                    if (IS_TCA_POST_CALL_ANALYTICS_ENABLED) {
                        configuration_event.PostCallAnalyticsSettings = {
                            OutputLocation: tcaOutputLocation,
                            DataAccessRoleArn: TCA_DATA_ACCESS_ROLE_ARN,
                        };
                        if (IS_CONTENT_REDACTION_ENABLED) {
                            configuration_event.PostCallAnalyticsSettings.ContentRedactionOutput =
                                POST_CALL_CONTENT_REDACTION_OUTPUT as ContentRedactionOutput;
                        }
                    }
                    yield { ConfigurationEvent: configuration_event };
                }
                if (audioInputStream == undefined) {
                    server.log.error(
                        `[TRANSCRIBING]: [${callMetaData.callId}] - audioInputStream undefined`
                    );
                    return;
                }
                // Do NOT iterate the shared audioInputStream directly. A
                // for-await over it propagates .return() (Node's
                // destroyOnReturn default) when THIS session's generator is
                // closed — which happens every time Transcribe closes the
                // result stream and we reconnect. That would destroy the single
                // audioInputStream the WebSocket handler keeps writing into,
                // throwing "Cannot call write after a stream was destroyed" and
                // breaking every subsequent reconnect. Instead pipe the shared
                // source into a per-session sink we own, iterate that, and on
                // teardown detach + destroy only the sink; the shared source
                // survives for the next session. Audio that arrives during the
                // brief reconnect gap buffers in the source and flows once the
                // next session re-pipes.
                const audioSink = new stream.PassThrough();
                audioInputStream.pipe(audioSink, { end: false });
                try {
                    for await (const chunk of audioSink) {
                        yield { AudioEvent: { AudioChunk: chunk } };
                    }
                } finally {
                    audioInputStream.unpipe(audioSink);
                    audioSink.destroy();
                }
            };

            let tsStream;
            let outputCallAnalyticsStream:
            | AsyncIterable<CallAnalyticsTranscriptResultStream>
            | undefined;
            let outputTranscriptStream: AsyncIterable<TranscriptResultStream> | undefined;

            const tsParams: transcriptionCommandInput<typeof isTCAEnabled> = {
                MediaSampleRateHertz: callMetaData.samplingRate,
                MediaEncoding: 'pcm',
                AudioStream: transcribeInput(),
            };

            if (sessionId) {
                tsParams.SessionId = sessionId;
                server.log.info(
                    `[TRANSCRIBING]: [${
                        callMetaData.callId
                    }] - Retry Transcribe streaming attempt ${retryCount} - use existing sessionId: ${sessionId}`
                );
            } else {
                server.log.info(
                    `[TRANSCRIBING]: [${
                        callMetaData.callId
                    }] - Initializing Transcribe streaming - no existing sessionId`
                );  
            }

            if (TRANSCRIBE_LANGUAGE_CODE === 'identify-language') {
                tsParams.IdentifyLanguage = true;
                if (TRANSCRIBE_LANGUAGE_OPTIONS) {
                    tsParams.LanguageOptions = TRANSCRIBE_LANGUAGE_OPTIONS.replace(/\s/g, '');
                    if (TRANSCRIBE_PREFERRED_LANGUAGE !== 'None') {
                        tsParams.PreferredLanguage =
                            TRANSCRIBE_PREFERRED_LANGUAGE as LanguageCode;
                    }
                }
            } else if (TRANSCRIBE_LANGUAGE_CODE === 'identify-multiple-languages') {
                (tsParams as StartStreamTranscriptionCommandInput).IdentifyMultipleLanguages = true;
                if (TRANSCRIBE_LANGUAGE_OPTIONS) {
                    tsParams.LanguageOptions = TRANSCRIBE_LANGUAGE_OPTIONS.replace(/\s/g, '');
                    if (TRANSCRIBE_PREFERRED_LANGUAGE !== 'None') {
                        tsParams.PreferredLanguage =
                            TRANSCRIBE_PREFERRED_LANGUAGE as LanguageCode;
                    }
                }
            } else {
                tsParams.LanguageCode = TRANSCRIBE_LANGUAGE_CODE as LanguageCode;
            }

            if (
                IS_CONTENT_REDACTION_ENABLED &&
            (TRANSCRIBE_LANGUAGE_CODE === 'en-US' ||
              TRANSCRIBE_LANGUAGE_CODE === 'en-AU' ||
              TRANSCRIBE_LANGUAGE_CODE === 'en-GB' ||
              TRANSCRIBE_LANGUAGE_CODE === 'es-US')
            ) {
                tsParams.ContentRedactionType =
                    CONTENT_REDACTION_TYPE as ContentRedactionType;
                if (TRANSCRIBE_PII_ENTITY_TYPES) {
                    tsParams.PiiEntityTypes = TRANSCRIBE_PII_ENTITY_TYPES;
                }
            }
            if (CUSTOM_VOCABULARY_NAME) {
                tsParams.VocabularyName = CUSTOM_VOCABULARY_NAME;
            }
            if (CUSTOM_LANGUAGE_MODEL_NAME) {
                tsParams.LanguageModelName = CUSTOM_LANGUAGE_MODEL_NAME;
            }

            if (isTCAEnabled) {
                server.log.debug(
                    `[TRANSCRIBING]: [${
                        callMetaData.callId
                    }] -StartCallAnalyticsStreamTranscriptionCommand args: ${JSON.stringify(
                        tsParams
                    )}`
                );
                const response = await transcribeClient.send(
                    new StartCallAnalyticsStreamTranscriptionCommand(
                        tsParams as StartCallAnalyticsStreamTranscriptionCommandInput
                    )
                );
                sessionId = response.SessionId;
                server.log.debug(
                    `[TRANSCRIBING]: [${callMetaData.callId}] === Received Initial response from TCA. Session Id: ${sessionId} ===`
                );

                outputCallAnalyticsStream = response.CallAnalyticsTranscriptResultStream;
            } else {
                (
                    tsParams as StartStreamTranscriptionCommandInput
                ).EnableChannelIdentification = true;
                (tsParams as StartStreamTranscriptionCommandInput).NumberOfChannels = 2;
                // ShowSpeakerLabel is a STREAM-level flag, so it goes on as soon
                // as EITHER channel wants diarization; writeTranscriptionSegment
                // then applies the labels only to the channel(s) that opted in.
                // Measured: enabling it does not change segmentation or accuracy
                // on the other channel, so the asymmetric cases cost nothing.
                // Note there is no MaxSpeakerLabels in the streaming API (batch
                // only) — speaker count is not tunable here.
                if (anyChannelDiarized(diarization)) {
                    (tsParams as StartStreamTranscriptionCommandInput).ShowSpeakerLabel = true;
                }
                server.log.debug(
                    `[TRANSCRIBING]: [${
                        callMetaData.callId
                    }] -Transcribe StartStreamTranscriptionCommand args: ${JSON.stringify(
                        tsParams
                    )}`
                );
                const response = await transcribeClient.send(
                    new StartStreamTranscriptionCommand(tsParams)
                );
                sessionId = response.SessionId;
                server.log.debug(
                    `[TRANSCRIBING]: [${callMetaData.callId}] === Received Initial response from Transcribe. Session Id: ${sessionId} ===`
                );

                outputTranscriptStream = response.TranscriptResultStream;
            }

            socketCallMap.startStreamTime = new Date();

            if (outputCallAnalyticsStream) {
                tsStream = stream.Readable.from(outputCallAnalyticsStream);
            } else if (outputTranscriptStream) {
                tsStream = stream.Readable.from(outputTranscriptStream);
            }

            if (tsStream) {
                for await (const event of tsStream) {
                    if (event.TranscriptEvent) {
                        const segmentMaxEndTime = await writeTranscriptionSegment(
                            event.TranscriptEvent,
                            callMetaData,
                            server,
                            timeOffsetSeconds,
                            diarization
                        );
                        if (segmentMaxEndTime > observedMaxEndTime) {
                            observedMaxEndTime = segmentMaxEndTime;
                        }
                    }
                }
            } else {
                throw new Error('Transcribe stream is empty');
            }

            // The results stream completed WITHOUT throwing. Amazon Transcribe
            // closes the HTTP/2 stream server-side on its own schedule (idle
            // behaviour, session duration limits, transient server close) — the
            // `for await` above then simply ends. Previously this returned and
            // transcription stopped permanently while the client kept streaming
            // audio into a dead pipe (GitHub #292). Unless the call has ended,
            // treat an unexpected clean close as a reconnect.
            if (!socketCallMap.ended) {
                const next = nextRetryCount();
                server.log.info(
                    `[TRANSCRIBING]: [${callMetaData.callId}] - Transcribe result stream closed unexpectedly; reconnecting. Retry count: ${next}, timeOffset: ${observedMaxEndTime.toFixed(2)}s`
                );
                await backoff(next);
                await startTranscribeSession(next, observedMaxEndTime);
            }
        } catch (error) {
            server.log.error(
                `[TRANSCRIBING]: [${
                    callMetaData.callId
                }] - Error in transcription session: ${normalizeErrorForLogging(error)}`
            );
            // A reused SessionId can be rejected once Transcribe has closed the
            // session server-side (e.g. "session ... has expired" / not found).
            // Reusing a stale id then fails every retry until MAX_RETRIES aborts
            // the whole meeting. Detect that class of error and drop the id so
            // the next attempt starts a FRESH session; the cumulative time
            // offset (below) keeps the transcript timeline continuous.
            if (isStaleSessionError(error)) {
                server.log.warn(
                    `[TRANSCRIBING]: [${callMetaData.callId}] - Transcribe SessionId '${sessionId}' is stale/expired; starting a fresh session on retry.`
                );
                sessionId = undefined;
            }
            const next = nextRetryCount();
            server.log.info(`[TRANSCRIBING]: [${callMetaData.callId}] - Attempting to restart session. Retry count: ${next}, timeOffset: ${observedMaxEndTime.toFixed(2)}s`);
            await backoff(next);
            await startTranscribeSession(next, observedMaxEndTime);
        }
    };

    await startTranscribeSession();
};

interface Segment {
    SegmentId: string;
    /** Final speaker string, already carrying any `(spk_N)` suffix. */
    Speaker: string;
    StartTime: number;
    EndTime: number;
    Transcript: string;
}

/**
 * Split one result into a segment per speaker turn, for a channel that has
 * diarization enabled.
 *
 * A single Transcribe result routinely spans several turns in natural
 * conversation (30s results with four turns are normal), and the turn structure
 * only exists in the per-item labels — so it has to be recovered here. Runs
 * shorter than the thresholds are absorbed into their neighbour first, because
 * single-word label flips are common and splitting on them would fragment
 * utterances. See the header of diarization.ts for the measurements behind this.
 *
 * Segment ids are keyed on Transcribe's own `ResultId` plus the run index, which
 * is what keeps a partial and its final aligned: partials carry NO labels, so a
 * partial is always a single run with index 0 and is cleanly replaced by the
 * final's first piece, while later pieces arrive as new segments. Keying on
 * anything derived from the label would orphan the partial.
 */
function buildDiarizedSegments(
    speakerName: string,
    result: Result,
    channelId: string,
    callMetadata: CallMetaData,
    server: FastifyInstance,
    timeOffsetSeconds: number
): Record<string, Segment> {
    const items = result.Alternatives?.[0]?.Items ?? [];
    const rawRuns = buildSpeakerRuns(items);
    const runs = smoothSpeakerRuns(rawRuns, diarizationRunThresholds);
    const isPartial = result.IsPartial === true;
    const resultId = result.ResultId ?? `${channelId}-${result.StartTime ?? 0}`;

    const segments: Record<string, Segment> = {};
    runs.forEach((run, index) => {
        const segmentId = `${resultId}-${channelId}-${index}`;
        segments[segmentId] = {
            SegmentId: segmentId,
            Speaker: appendSpeakerLabel(speakerName, run.label),
            StartTime: run.startTime + timeOffsetSeconds,
            EndTime: run.endTime + timeOffsetSeconds,
            Transcript: runTranscript(run),
        };
    });

    recordDiarizationDiagnostics(callMetadata, server, channelId, result, rawRuns.length, items);

    // One line per FINAL result, at info: this is the record needed to re-tune
    // the thresholds from a real meeting, and it is low volume (a handful per
    // minute of audio). Transcript text is deliberately left to debug.
    const summary =
        `[DIARIZATION]: [${callMetadata.callId}] - ${channelId} result ${resultId.slice(0, 8)}` +
        ` ${isPartial ? 'partial' : 'final'}: runs [${describeRuns(rawRuns)}]` +
        ` -> ${runs.length} segment(s) [${runs.map((r) => r.label ?? 'unlabelled').join(', ')}]` +
        (rawRuns.length !== runs.length ? ` (absorbed ${rawRuns.length - runs.length})` : '');
    if (isPartial) {
        server.log.debug(summary);
    } else {
        server.log.info(summary);
    }
    if (DEBUG) {
        for (const run of runs) {
            server.log.debug(
                `[DIARIZATION]: [${callMetadata.callId}] - ${channelId} ${run.label ?? 'unlabelled'}` +
                    ` [${run.startTime.toFixed(2)}-${run.endTime.toFixed(2)}] ${runTranscript(run)}`
            );
        }
        server.log.debug(
            `[DIARIZATION]: [${callMetadata.callId}] - ${channelId} item labels: ` +
                items
                    .map((i) => `${i.Content ?? ''}[${formatSpeakerLabel(i.Speaker) ?? '-'}]`)
                    .join(' ')
        );
    }
    return segments;
}

/**
 * Track whether Transcribe is actually returning labels, and warn ONCE per call
 * if it never does.
 *
 * Speaker partitioning is gated per language: on an unsupported language the API
 * accepts `ShowSpeakerLabel` and simply returns no labels, so the feature
 * silently does nothing. Without this the only symptom is an absence, which is
 * exactly what nobody notices.
 */
function recordDiarizationDiagnostics(
    callMetadata: CallMetaData,
    server: FastifyInstance,
    channelId: string,
    result: Result,
    runCount: number,
    items: Item[]
): void {
    if (result.IsPartial === true) {
        return; // only finals carry labels, so only finals are evidence
    }
    if (!callMetadata.diarizationDiagnostics) {
        callMetadata.diarizationDiagnostics = { finals: 0, labelled: 0, warned: false };
    }
    const diag = callMetadata.diarizationDiagnostics;
    diag.finals += 1;
    if (items.some((i) => formatSpeakerLabel(i.Speaker) !== undefined)) {
        diag.labelled += 1;
    }
    const NO_LABEL_WARN_AFTER_FINALS = 3;
    if (!diag.warned && diag.labelled === 0 && diag.finals >= NO_LABEL_WARN_AFTER_FINALS) {
        diag.warned = true;
        server.log.warn(
            `[DIARIZATION]: [${callMetadata.callId}] - Speaker partitioning is enabled for ${channelId}` +
                ` but Amazon Transcribe has returned NO speaker labels across ${diag.finals} final` +
                ' results. Speaker partitioning is not supported for every transcription language —' +
                ' check TRANSCRIBE_LANGUAGE_CODE. Segments will carry unlabelled speaker names.' +
                ` (runs seen: ${runCount})`
        );
    }
}

function processTranscriptionResults(
    speakerName: string,
    result: Result,
    callMetadata: CallMetaData,
    server: FastifyInstance,
    timeOffsetSeconds = 0,
    diarization?: DiarizationSettings
): Record<string, Segment> {
    const channelId = result.ChannelId ?? CHANNEL_SYSTEM;
    if (diarizationEnabledFor(channelId, diarization)) {
        return buildDiarizedSegments(
            speakerName,
            result,
            channelId,
            callMetadata,
            server,
            timeOffsetSeconds
        );
    }

    const segments: Record<string, Segment> = {};

    // Initialize channel data if it doesn't exist
    if (!callMetadata.channels) {
        callMetadata.channels = {};
    }
    if (!callMetadata.channels[channelId]) {
        callMetadata.channels[channelId] = {
            currentSpeakerName: null,
            speakers: [],
            startTimes: [],
        };
    }

    const channelData = callMetadata.channels[channelId];

    if (channelData.currentSpeakerName !== speakerName) {
        channelData.currentSpeakerName = speakerName;
        channelData.speakers.push(speakerName);
        const lastItem = result.Alternatives?.[0]?.Items?.[result.Alternatives[0].Items.length - 1];
        if (lastItem) {
            // Store absolute (offset-adjusted) start time so segment ids and
            // ordering stay monotonic across reconnects (see startTranscribe).
            channelData.startTimes.push((lastItem.StartTime ?? 0) + timeOffsetSeconds);
        }
    }

    const alternative = result.Alternatives?.[0];
    if (alternative?.Items) {
        for (const item of alternative.Items) {
            addItemToSegment(item, segments, channelData, channelId, timeOffsetSeconds);
            if (DEBUG) {
                server.log.debug(`[${callMetadata.callId}] Item ${item.StartTime}, ${item.EndTime}, ${item.Content}`);
                server.log.debug(`[${callMetadata.callId}] Speakers ${JSON.stringify(channelData.speakers)}`);
                server.log.debug(`[${callMetadata.callId}] Starttimes ${JSON.stringify(channelData.startTimes)}`);
                server.log.debug(`[${callMetadata.callId}] Segments ${JSON.stringify(segments)}`);
            }
        }
    }
  
    if (!result.IsPartial) {
        server.log.debug(`[${callMetadata.callId}] Non partial result - Resetting channel speaker data for ${channelId}`);
        channelData.currentSpeakerName = null;
        channelData.speakers = [];
        channelData.startTimes = [];
    }
  
    return segments;
}
  
/**
 * Bin an item into a segment keyed by the CLIENT-reported speaker (activeSpeaker /
 * agentId). Used only when diarization is off for the channel — a diarized
 * channel is segmented by Transcribe's own speaker turns instead, in
 * buildDiarizedSegments.
 */
function addItemToSegment(
    item: Item,
    segments: Record<string, Segment>,
    channelData: ChannelSpeakerData,
    channelId: string,
    timeOffsetSeconds = 0
): void {
    const { speakers, startTimes } = channelData;
    // startTimes are already absolute (offset-adjusted); compare against the
    // item's absolute start time so the speaker lookup stays correct.
    const itemStart = (item.StartTime ?? 0) + timeOffsetSeconds;
    const itemEnd = (item.EndTime ?? 0) + timeOffsetSeconds;
    let index = startTimes.findIndex((time) => time > itemStart);
    if (index == -1) {
        // -1 means item.Starttime is greater than all speaker startimes, so use the last speaker
        index = startTimes.length - 1;
    } else if (index > 0) {
        // choose prior speaker starttime, unless we're already at the start of the list
        index = index - 1;
    }
    const segmentId = `${speakers[index] ?? 'unknown'}-${startTimes[index] ?? 'unknown'}-${channelId}`;

    if (!segments[segmentId]) {
        segments[segmentId] = {
            SegmentId: segmentId,
            Speaker: speakers[index] ?? 'unknown',
            StartTime: startTimes[index] ?? 0,
            EndTime: itemEnd,
            Transcript: '',
        };
    } else if (item.Type === 'pronunciation') {
        segments[segmentId].Transcript += ' ';
    }

    segments[segmentId].EndTime = itemEnd;
    segments[segmentId].Transcript += item.Content;
}

// Returns the highest absolute segment EndTime (seconds) written by this call,
// so the caller can advance the cumulative reconnect offset. Returns
// timeOffsetSeconds unchanged when nothing was written.
export const writeTranscriptionSegment = async function (
    transcribeMessageJson: TranscriptEvent,
    callMetadata: CallMetaData,
    server: FastifyInstance,
    timeOffsetSeconds = 0,
    diarization?: DiarizationSettings
): Promise<number> {
    let maxEndTime = timeOffsetSeconds;
    if (
        transcribeMessageJson.Transcript?.Results &&
    transcribeMessageJson.Transcript?.Results.length > 0
    ) {
        const result = transcribeMessageJson.Transcript.Results[0];
        if (result.Alternatives && result.Alternatives.length > 0) {
            const speakerName =
                result.ChannelId === CHANNEL_SYSTEM
                    ? callMetadata.activeSpeaker
                    : callMetadata?.agentId ?? 'n/a';
            const segments = processTranscriptionResults(
                speakerName,
                result,
                callMetadata,
                server,
                timeOffsetSeconds,
                diarization
            );

            for (const segment of Object.values(segments)) {
                if (segment.EndTime > maxEndTime) {
                    maxEndTime = segment.EndTime;
                }
                const now = new Date().toISOString();
                const kdsObject: AddTranscriptSegmentEvent = {
                    EventType: 'ADD_TRANSCRIPT_SEGMENT',
                    CallId: callMetadata.callId,
                    Channel: result.ChannelId === CHANNEL_SYSTEM ? 'CALLER' : 'AGENT',
                    SegmentId: segment.SegmentId,
                    StartTime: segment.StartTime,
                    EndTime: segment.EndTime,
                    Transcript: segment.Transcript,
                    IsPartial: result.IsPartial,
                    CreatedAt: now,
                    UpdatedAt: now,
                    Sentiment: undefined,
                    TranscriptEvent: undefined,
                    UtteranceEvent: undefined,
                    // Already carries any `(spk_N)` suffix — see
                    // buildDiarizedSegments. Identical to the pre-feature value
                    // when the channel has diarization off.
                    Speaker: segment.Speaker,
                    AccessToken: callMetadata.accessToken,
                    IdToken: callMetadata.idToken,
                    RefreshToken: callMetadata.refreshToken,
                };

                const putParams = {
                    StreamName: kdsStreamName,
                    PartitionKey: callMetadata.callId,
                    Data: Buffer.from(JSON.stringify(kdsObject)),
                };

                const putCmd = new PutRecordCommand(putParams);
                try {
                    await kinesisClient.send(putCmd);
                    server.log.debug(
                        `[${kdsObject.EventType}]: [${callMetadata.callId}] - Written ${
                            kdsObject.EventType
                        } event to KDS: ${JSON.stringify(kdsObject)}`
                    );
                } catch (error) {
                    server.log.error(
                        `[${kdsObject.EventType}]: [${
                            callMetadata.callId
                        }] - Error writing ${
                            kdsObject.EventType
                        } to KDS : ${normalizeErrorForLogging(
                            error
                        )} KDS object: ${JSON.stringify(kdsObject)}`
                    );
                }
            }
        }
    }
    return maxEndTime;
};
