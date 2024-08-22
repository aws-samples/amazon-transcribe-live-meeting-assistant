// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
    AddTranscriptSegmentEvent,
    SocketCallData,
    CallMetaData,
} from './eventtypes';

import { normalizeErrorForLogging } from '../utils';

const formatPath = function (path: string) {
    let pathOut = path;
    if (path.length > 0 && path.charAt(path.length - 1) != '/') {
        pathOut += '/';
    }
    return pathOut;
};

import dotenv from 'dotenv';
dotenv.config();

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
const showSpeakerLabel =
  (process.env['SHOW_SPEAKER_LABEL'] || 'true') === 'true';

const tcaOutputLocation = `s3://${RECORDINGS_BUCKET_NAME}/${CALL_ANALYTICS_FILE_PREFIX}`;

type transcriptionCommandInput<TCAEnabled> = TCAEnabled extends true
    ? StartCallAnalyticsStreamTranscriptionCommandInput
    : StartStreamTranscriptionCommandInput;

const kinesisClient = new KinesisClient({ region: AWS_REGION });
const transcribeClient = new TranscribeStreamingClient({ region: AWS_REGION });

export const writeCallEvent = async (
    callEvent: CallStartEvent | CallEndEvent | CallRecordingEvent,
    server: FastifyInstance
) => {
    const putParams = {
        StreamName: kdsStreamName,
        PartitionKey: callEvent.CallId,
        Data: Buffer.from(JSON.stringify(callEvent)),
    };

    const putCmd = new PutRecordCommand(putParams);
    try {
        kinesisClient.send(putCmd);
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
    };
    await writeCallEvent(callRecordingEvent, server);
};

export const startTranscribe = async (
    socketCallMap: SocketCallData,
    server: FastifyInstance
) => {
    const callMetaData = socketCallMap.callMetadata;
    const audioInputStream = socketCallMap.audioInputStream;

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
        if (audioInputStream != undefined) {
            for await (const chunk of audioInputStream) {
                yield { AudioEvent: { AudioChunk: chunk } };
                // yield { AudioEvent: { AudioChunk:Uint8Array.from(new Array(2).fill([0x00, 0x00]).flat()), EndOfStream: true } };
            }
        } else {
            server.log.error(
                `[TRANSCRIBING]: [${callMetaData.callId}] - audioInputStream undefined`
            );
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
        tsParams.IdentifyMultipleLanguages = true;
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
        try {
            const response = await transcribeClient.send(
                new StartCallAnalyticsStreamTranscriptionCommand(
                    tsParams as StartCallAnalyticsStreamTranscriptionCommandInput
                )
            );
            server.log.debug(
                `[TRANSCRIBING]: [${callMetaData.callId}] === Received Initial response from TCA. Session Id: ${response.SessionId} ===`
            );

            outputCallAnalyticsStream = response.CallAnalyticsTranscriptResultStream;
        } catch (err) {
            server.log.error(
                `[TRANSCRIBING]: [${
                    callMetaData.callId
                }] - Error in StartCallAnalyticsStreamTranscriptionCommand: ${normalizeErrorForLogging(
                    err
                )}`
            );
            return;
        }
    } else {
        (
            tsParams as StartStreamTranscriptionCommandInput
        ).EnableChannelIdentification = true;
        (tsParams as StartStreamTranscriptionCommandInput).NumberOfChannels = 2;
        if (showSpeakerLabel) {
            tsParams.ShowSpeakerLabel = true;
        }
        server.log.debug(
            `[TRANSCRIBING]: [${
                callMetaData.callId
            }] -Transcribe StartStreamTranscriptionCommand args: ${JSON.stringify(
                tsParams
            )}`
        );
        try {
            const response = await transcribeClient.send(
                new StartStreamTranscriptionCommand(tsParams)
            );
            server.log.debug(
                `[TRANSCRIBING]: [${callMetaData.callId}] === Received Initial response from Transcribe. Session Id: ${response.SessionId} ===`
            );

            outputTranscriptStream = response.TranscriptResultStream;
        } catch (err) {
            server.log.error(
                `[TRANSCRIBING]: [${
                    callMetaData.callId
                }] - Error in StartStreamTranscription: ${normalizeErrorForLogging(
                    err
                )}`
            );
            return;
        }
    }
    socketCallMap.startStreamTime = new Date();

    if (outputCallAnalyticsStream) {
        tsStream = stream.Readable.from(outputCallAnalyticsStream);
    } else if (outputTranscriptStream) {
        tsStream = stream.Readable.from(outputTranscriptStream);
    }

    try {
        if (tsStream) {
            for await (const event of tsStream) {
                if (event.TranscriptEvent) {
                    await writeTranscriptionSegment(
                        event.TranscriptEvent,
                        callMetaData,
                        server
                    );
                }
            }
        } else {
            server.log.error(
                `[TRANSCRIBING]: [${callMetaData.callId}] - Transcribe stream is empty`
            );
        }
    } catch (error) {
        server.log.error(
            `[TRANSCRIBING]: [${
                callMetaData.callId
            }] - Error processing Transcribe results stream ${normalizeErrorForLogging(
                error
            )}`
        );
    } finally {
    // writeCallEndEvent(callMetaData);
    }
};

// Extend the CallMetaData interface
interface ExtendedCallMetaData extends CallMetaData {
    currentSpeakerName?: string | null;
    speakers?: string[];
    startTimes?: number[];
}

interface Segment {
    SegmentId: string;
    Speaker: string;
    StartTime: number;
    EndTime: number;
    Transcript: string;
}

function addItemToSegment(
    item: Item,
    segments: Record<string, Segment>,
    callMetadata: ExtendedCallMetaData
): Record<string, Segment> {
    const startTimes = callMetadata.startTimes ?? [];
    const speakers = callMetadata.speakers ?? [];
    const segmentIndex = startTimes.findIndex((time) => time > (item.StartTime ?? 0)) - 1;
    const index = segmentIndex < 0 ? 0 : segmentIndex;
    const segmentId = `${speakers[index] ?? 'unknown'}-${startTimes[index] ?? 'unknown'}`;
    if (!segments[segmentId]) {
        segments[segmentId] = {
            SegmentId: segmentId,
            Speaker: speakers[index] ?? 'unknown',
            StartTime: startTimes[index] ?? 0,
            EndTime: item.EndTime ?? 0,
            Transcript: '',
        };
    } else if (item.Type === 'pronunciation') {
        segments[segmentId].Transcript += ' ';
    }
    segments[segmentId].EndTime = item.EndTime ?? 0;
    segments[segmentId].Transcript += item.Content;
    return segments;
}


function processTranscriptionResults(
    speakerName: string,
    result: Result,
    callMetadata: ExtendedCallMetaData
): Record<string, Segment> {
    const segments: Record<string, Segment> = {};
    if (callMetadata.currentSpeakerName !== speakerName) {
        callMetadata.currentSpeakerName = speakerName;
        callMetadata.speakers = callMetadata.speakers || [];
        callMetadata.startTimes = callMetadata.startTimes || [];
        callMetadata.speakers.push(speakerName);
        const lastItem =
            result.Alternatives?.[0]?.Items?.[result.Alternatives?.[0]?.Items?.length - 1];
        if (lastItem) {
            callMetadata.startTimes.push(lastItem.StartTime ?? 0);
        }
    }

    const alternative = result.Alternatives?.[0];
    if (alternative?.Items) {
        for (const item of alternative.Items) {
            addItemToSegment(item, segments, callMetadata);
        }
    }

    if (!result.IsPartial) {
        callMetadata.currentSpeakerName = null;
        callMetadata.speakers = [];
        callMetadata.startTimes = [];
    }

    return segments;
}

export const writeTranscriptionSegment = async function (
    transcribeMessageJson: TranscriptEvent,
    callMetadata: ExtendedCallMetaData,
    server: FastifyInstance
) {
    if (
        transcribeMessageJson.Transcript?.Results &&
    transcribeMessageJson.Transcript?.Results.length > 0
    ) {
        const result = transcribeMessageJson.Transcript.Results[0];
        if (result.Alternatives && result.Alternatives.length > 0) {
            const speakerName =
        result.ChannelId === 'ch_0'
            ? callMetadata.activeSpeaker
            : callMetadata?.agentId ?? 'n/a';
            const segments = processTranscriptionResults(
                speakerName,
                result,
                callMetadata
            );

            for (const segment of Object.values(segments)) {
                const now = new Date().toISOString();
                const kdsObject: AddTranscriptSegmentEvent = {
                    EventType: 'ADD_TRANSCRIPT_SEGMENT',
                    CallId: callMetadata.callId,
                    Channel: result.ChannelId === 'ch_0' ? 'CALLER' : 'AGENT',
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
                    Speaker: segment.Speaker,
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
};
