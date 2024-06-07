// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { FastifyInstance } from 'fastify';
import stream from 'stream';

import {
    TranscriptEvent,
    UtteranceEvent,
    CategoryEvent,
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
    Result,
} from '@aws-sdk/client-transcribe-streaming';


import {
    KinesisClient,
    PutRecordCommand
} from '@aws-sdk/client-kinesis';

import {
    CallStartEvent,
    CallEndEvent,
    CallRecordingEvent,
    AddTranscriptSegmentEvent,
    AddCallCategoryEvent,
    SocketCallData,
    CallMetaData,
} from './eventtypes';

import {
    normalizeErrorForLogging
} from '../utils';


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
const TRANSCRIBE_LANGUAGE_CODE = process.env['TRANSCRIBE_LANGUAGE_CODE'] || 'en-US';
const TRANSCRIBE_LANGUAGE_OPTIONS = process.env['TRANSCRIBE_LANGUAGE_OPTIONS'] || undefined;
const TRANSCRIBE_PREFERRED_LANGUAGE = process.env['TRANSCRIBE_PREFERRED_LANGUAGE'] || 'None';
const CUSTOM_VOCABULARY_NAME = process.env['CUSTOM_VOCABULARY_NAME'] || undefined;
const CUSTOM_LANGUAGE_MODEL_NAME = process.env['CUSTOM_LANGUAGE_MODEL_NAME'] || undefined;
const IS_CONTENT_REDACTION_ENABLED = (process.env['IS_CONTENT_REDACTION_ENABLED'] || '') === 'true';
const CONTENT_REDACTION_TYPE = process.env['CONTENT_REDACTION_TYPE'] || 'PII';
const TRANSCRIBE_PII_ENTITY_TYPES = process.env['TRANSCRIBE_PII_ENTITY_TYPES'] || undefined;
const TCA_DATA_ACCESS_ROLE_ARN = process.env['TCA_DATA_ACCESS_ROLE_ARN'] || '';
const CALL_ANALYTICS_FILE_PREFIX = formatPath(process.env['CALL_ANALYTICS_FILE_PREFIX'] || 'lca-call-analytics-json/');
const RECORDINGS_BUCKET_NAME = process.env['RECORDINGS_BUCKET_NAME'] || null;
// optional - disable post call analytics output
const IS_TCA_POST_CALL_ANALYTICS_ENABLED = (process.env['IS_TCA_POST_CALL_ANALYTICS_ENABLED'] || 'false') === 'true';
// optional - when redaction is enabled, choose 'redacted' only (dafault), or 'redacted_and_unredacted' for both
const POST_CALL_CONTENT_REDACTION_OUTPUT = process.env['POST_CALL_CONTENT_REDACTION_OUTPUT'] || 'redacted';

const savePartial = (process.env['SAVE_PARTIAL_TRANSCRIPTS'] || 'true') === 'true';
const kdsStreamName = process.env['KINESIS_STREAM_NAME'] || '';
const showSpeakerLabel = (process.env['SHOW_SPEAKER_LABEL'] || 'true') === 'true';

const tcaOutputLocation = `s3://${RECORDINGS_BUCKET_NAME}/${CALL_ANALYTICS_FILE_PREFIX}`;

type transcriptionCommandInput<TCAEnabled> = TCAEnabled extends true
    ? StartCallAnalyticsStreamTranscriptionCommandInput
    : StartStreamTranscriptionCommandInput;

const kinesisClient = new KinesisClient({ region: AWS_REGION });
const transcribeClient = new TranscribeStreamingClient({ region: AWS_REGION });

export const writeCallEvent = async (callEvent: CallStartEvent | CallEndEvent | CallRecordingEvent, server: FastifyInstance) => {

    const putParams = {
        StreamName: kdsStreamName,
        PartitionKey: callEvent.CallId,
        Data: Buffer.from(JSON.stringify(callEvent))
    };

    const putCmd = new PutRecordCommand(putParams);
    try {
        kinesisClient.send(putCmd);
        server.log.debug(`[${callEvent.EventType}]: ${callEvent.CallId} - Written ${callEvent.EventType} Event to KDS: ${JSON.stringify(callEvent)}`);
    } catch (error) {
        server.log.debug(`[${callEvent.EventType}]: ${callEvent.CallId} - Error writing ${callEvent.EventType} Call Event to KDS : ${normalizeErrorForLogging(error)} Event: ${JSON.stringify(callEvent)}`);
    }
};

export const writeCallStartEvent = async (callMetaData: CallMetaData, server: FastifyInstance): Promise<void> => {
    const callStartEvent: CallStartEvent = {
        EventType: 'START',
        CallId: callMetaData.callId,
        CustomerPhoneNumber: callMetaData.fromNumber || 'Customer Phone',
        SystemPhoneNumber: callMetaData.toNumber || 'System Phone',
        AgentId: callMetaData.agentId,
        CreatedAt: new Date().toISOString()
    };
    await writeCallEvent(callStartEvent, server);
};

export const writeCallEndEvent = async (callMetaData: CallMetaData, server: FastifyInstance): Promise<void> => {
    const callEndEvent: CallEndEvent = {
        EventType: 'END',
        CallId: callMetaData.callId,
        CustomerPhoneNumber: callMetaData.fromNumber || 'Customer Phone',
        SystemPhoneNumber: callMetaData.toNumber || 'System Phone',
    };
    await writeCallEvent(callEndEvent, server);
};

export const writeCallRecordingEvent = async (callMetaData: CallMetaData, recordingUrl: string, server: FastifyInstance): Promise<void> => {
    const callRecordingEvent: CallRecordingEvent = {
        EventType: 'ADD_S3_RECORDING_URL',
        CallId: callMetaData.callId,
        RecordingUrl: recordingUrl
    };
    await writeCallEvent(callRecordingEvent, server);
};


export const startTranscribe = async (socketCallMap: SocketCallData, server: FastifyInstance) => {

    const callMetaData = socketCallMap.callMetadata;
    const audioInputStream = socketCallMap.audioInputStream;

    server.log.debug(`[${callMetaData.callEvent}]: [${callMetaData.callId}] - Starting transcribe:  ${JSON.stringify(callMetaData)}`);
    const transcribeInput = async function* () {
        if (isTCAEnabled) {
            const channel0: ChannelDefinition = { ChannelId: 0, ParticipantRole: ParticipantRole.CUSTOMER };
            const channel1: ChannelDefinition = { ChannelId: 1, ParticipantRole: ParticipantRole.AGENT };
            const channel_definitions: ChannelDefinition[] = [];
            channel_definitions.push(channel0);
            channel_definitions.push(channel1);
            const configuration_event: ConfigurationEvent = { ChannelDefinitions: channel_definitions };
            if (IS_TCA_POST_CALL_ANALYTICS_ENABLED) {
                configuration_event.PostCallAnalyticsSettings = {
                    OutputLocation: tcaOutputLocation,
                    DataAccessRoleArn: TCA_DATA_ACCESS_ROLE_ARN
                };
                if (IS_CONTENT_REDACTION_ENABLED) {
                    configuration_event.PostCallAnalyticsSettings.ContentRedactionOutput = POST_CALL_CONTENT_REDACTION_OUTPUT as ContentRedactionOutput;
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
            server.log.error(`[TRANSCRIBING]: [${callMetaData.callId}] - audioInputStream undefined`);
        }
    };

    let tsStream;
    let outputCallAnalyticsStream: AsyncIterable<CallAnalyticsTranscriptResultStream> | undefined;
    let outputTranscriptStream: AsyncIterable<TranscriptResultStream> | undefined;

    const tsParams: transcriptionCommandInput<typeof isTCAEnabled> = {
        MediaSampleRateHertz: callMetaData.samplingRate,
        MediaEncoding: 'pcm',
        AudioStream: transcribeInput()
    };

    if (TRANSCRIBE_LANGUAGE_CODE === 'identify-language') {
        tsParams.IdentifyLanguage = true;
        if (TRANSCRIBE_LANGUAGE_OPTIONS) {
            tsParams.LanguageOptions = TRANSCRIBE_LANGUAGE_OPTIONS.replace(/\s/g, '');
            if (TRANSCRIBE_PREFERRED_LANGUAGE !== 'None') {
                tsParams.PreferredLanguage = TRANSCRIBE_PREFERRED_LANGUAGE as LanguageCode;
            }
        }
    } else if (TRANSCRIBE_LANGUAGE_CODE === 'identify-multiple-languages') {
        tsParams.IdentifyMultipleLanguages = true;
        if (TRANSCRIBE_LANGUAGE_OPTIONS) {
            tsParams.LanguageOptions = TRANSCRIBE_LANGUAGE_OPTIONS.replace(/\s/g, '');
            if (TRANSCRIBE_PREFERRED_LANGUAGE !== 'None') {
                tsParams.PreferredLanguage = TRANSCRIBE_PREFERRED_LANGUAGE as LanguageCode;
            }
        }
    } else {
        tsParams.LanguageCode = TRANSCRIBE_LANGUAGE_CODE as LanguageCode;
    }

    if (IS_CONTENT_REDACTION_ENABLED && TRANSCRIBE_LANGUAGE_CODE === 'en-US') {
        tsParams.ContentRedactionType = CONTENT_REDACTION_TYPE as ContentRedactionType;
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
        try {
            const response = await transcribeClient.send(
                new StartCallAnalyticsStreamTranscriptionCommand(tsParams as StartCallAnalyticsStreamTranscriptionCommandInput)
            );
            server.log.debug(`[TRANSCRIBING]: [${callMetaData.callId}] === Received Initial response from TCA. Session Id: ${response.SessionId} ===`);

            outputCallAnalyticsStream = response.CallAnalyticsTranscriptResultStream;
        } catch (err) {
            server.log.error(`[TRANSCRIBING]: [${callMetaData.callId}] - Error in StartCallAnalyticsStreamTranscriptionCommand: ${normalizeErrorForLogging(err)}`);
            return;
        }
    } else {
        (tsParams as StartStreamTranscriptionCommandInput).EnableChannelIdentification = true;
        (tsParams as StartStreamTranscriptionCommandInput).NumberOfChannels = 2;
        if (showSpeakerLabel) {
            tsParams.ShowSpeakerLabel = true;
        }
        try {
            const response = await transcribeClient.send(
                new StartStreamTranscriptionCommand(tsParams)
            );
            server.log.debug(`[TRANSCRIBING]: [${callMetaData.callId}] === Received Initial response from Transcribe. Session Id: ${response.SessionId} ===`);

            outputTranscriptStream = response.TranscriptResultStream;
        } catch (err) {
            server.log.error(`[TRANSCRIBING]: [${callMetaData.callId}] - Error in StartStreamTranscription: ${normalizeErrorForLogging(err)}`);
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
                    if (showSpeakerLabel) {
                        const events = splitTranscriptEventBySpeaker(event.TranscriptEvent);
                        for (const transcriptEvent of events) {
                            await writeTranscriptionSegment(transcriptEvent, callMetaData, server);
                        }
                    } else {
                        await writeTranscriptionSegment(event.TranscriptEvent, callMetaData, server);
                    }
                }
                if (event.CategoryEvent && event.CategoryEvent.MatchedCategories) {
                    await writeAddCallCategoryEvent(event.CategoryEvent, callMetaData, server);
                }
                if (event.UtteranceEvent && event.UtteranceEvent.UtteranceId) {
                    await writeUtteranceEvent(event.UtteranceEvent, callMetaData, server);
                }
            }
        } else {
            server.log.error(`[TRANSCRIBING]: [${callMetaData.callId}] - Transcribe stream is empty`);
        }
    } catch (error) {
        server.log.error(`[TRANSCRIBING]: [${callMetaData.callId}] - Error processing Transcribe results stream ${normalizeErrorForLogging(error)}`);
    } finally {
        // writeCallEndEvent(callMetaData);
    }
};


export const writeTranscriptionSegment = async function (transcribeMessageJson: TranscriptEvent, callMetadata: CallMetaData, server: FastifyInstance) {
    if (transcribeMessageJson.Transcript?.Results && transcribeMessageJson.Transcript?.Results.length > 0) {
        if (transcribeMessageJson.Transcript?.Results[0].Alternatives && transcribeMessageJson.Transcript?.Results[0].Alternatives?.length > 0) {

            const result = transcribeMessageJson.Transcript?.Results[0];

            if (result.IsPartial == undefined || (result.IsPartial == true && !savePartial)) {
                return;
            }
            const { Transcript: transcript } = transcribeMessageJson.Transcript.Results[0].Alternatives[0];
            const now = new Date().toISOString();

            const kdsObject: AddTranscriptSegmentEvent = {
                EventType: 'ADD_TRANSCRIPT_SEGMENT',
                CallId: callMetadata.callId,
                Channel: (result.ChannelId === 'ch_0' ? 'CALLER' : 'AGENT'),
                SegmentId: `${result.ChannelId}-${result.StartTime}`,
                StartTime: result.StartTime || 0,
                EndTime: result.EndTime || 0,
                Transcript: transcript || '',
                IsPartial: result.IsPartial,
                CreatedAt: now,
                UpdatedAt: now,
                Sentiment: undefined,
                TranscriptEvent: undefined,
                UtteranceEvent: undefined,
                Speaker: (result.ChannelId === 'ch_0' ? callMetadata.activeSpeaker : (callMetadata?.agentId ?? 'n/a'))
            };

            const putParams = {
                StreamName: kdsStreamName,
                PartitionKey: callMetadata.callId,
                Data: Buffer.from(JSON.stringify(kdsObject)),
            };

            const putCmd = new PutRecordCommand(putParams);
            try {
                kinesisClient.send(putCmd);
                server.log.debug(`[${kdsObject.EventType}]: [${callMetadata.callId}] - Written ${kdsObject.EventType} event to KDS: ${JSON.stringify(kdsObject)}`);
            } catch (error) {
                server.log.error(`[${kdsObject.EventType}]: [${callMetadata.callId}] - Error writing ${kdsObject.EventType} to KDS : ${normalizeErrorForLogging(error)} KDS object: ${JSON.stringify(kdsObject)}`);
            }
        }
    }
};

export const writeUtteranceEvent = async function (utteranceEvent: UtteranceEvent, callMetadata: CallMetaData, server: FastifyInstance) {
    let isCustomer = false;

    if (utteranceEvent) {
        if (utteranceEvent.ParticipantRole === ParticipantRole.CUSTOMER) {
            isCustomer = true;
        } else {
            isCustomer = false;
        }
        if (utteranceEvent.IsPartial == undefined || (utteranceEvent.IsPartial == true && !savePartial)) {
            return;
        }
    }

    const now = new Date().toISOString();

    const kdsObject: AddTranscriptSegmentEvent = {
        EventType: 'ADD_TRANSCRIPT_SEGMENT',
        CallId: callMetadata.callId,
        TranscriptEvent: undefined,
        UtteranceEvent: utteranceEvent,
        CreatedAt: now,
        UpdatedAt: now,
        Speaker: (isCustomer ? callMetadata.activeSpeaker : (callMetadata?.agentId ?? 'n/a'))
    };

    const putParams = {
        StreamName: kdsStreamName,
        PartitionKey: callMetadata.callId,
        Data: Buffer.from(JSON.stringify(kdsObject)),
    };

    const putCmd = new PutRecordCommand(putParams);
    try {
        kinesisClient.send(putCmd);
        server.log.debug(`[${kdsObject.EventType}]: [${callMetadata.callId}] - Written ${kdsObject.EventType} event to KDS: ${JSON.stringify(kdsObject)}`);
    } catch (error) {
        server.log.error(`[${kdsObject.EventType}]: [${callMetadata.callId}}] - Error writing ${kdsObject.EventType} to KDS : ${normalizeErrorForLogging(error)} KDS object: ${JSON.stringify(kdsObject)}`);
    }
};

export const writeAddCallCategoryEvent = async function (categoryEvent: CategoryEvent, callMetaData: CallMetaData, server: FastifyInstance) {

    if (categoryEvent) {
        const now = new Date().toISOString();

        const kdsObject: AddCallCategoryEvent = {
            EventType: 'ADD_CALL_CATEGORY',
            CallId: callMetaData.callId,
            CategoryEvent: categoryEvent,
            CreatedAt: now,
        };

        const putParams = {
            StreamName: kdsStreamName,
            PartitionKey: callMetaData.callId,
            Data: Buffer.from(JSON.stringify(kdsObject)),
        };

        const putCmd = new PutRecordCommand(putParams);
        try {
            kinesisClient.send(putCmd);
            server.log.debug(`[${kdsObject.EventType}]: [${callMetaData.callId}] - Written ${kdsObject.EventType} event to KDS: ${JSON.stringify(kdsObject)}`);
        } catch (error) {
            server.log.error(`[${kdsObject.EventType}]: [${callMetaData.callId}] - Error writing ${kdsObject.EventType} to KDS : ${normalizeErrorForLogging(error)} KDS object: ${JSON.stringify(kdsObject)}`);
        }

    }
};

export const concatItemsIntoTranscript = (items: Item[]) => {
    let text = '';
    items.forEach(item => {
        if (item.Type === 'punctuation') {
            text = text.trim();
        }
        text += item.Content + ' ';
    });

    // Trim last space
    text = text.trim();
    return text;
};

export const splitTranscriptEventBySpeaker = (transcript: TranscriptEvent): TranscriptEvent[] => {
    const itemsBySpeaker: { [key: string]: Item[] } = {};

    let initialSpeaker: string;
    let lastSpeaker: string;
    let firstResult: Result;
    if (transcript.Transcript &&
        transcript.Transcript.Results &&
        transcript.Transcript.Results[0] &&
        transcript.Transcript.Results[0].Alternatives &&
        transcript.Transcript.Results[0].Alternatives[0] &&
        transcript.Transcript.Results[0].Alternatives[0].Items) {

        firstResult = transcript.Transcript.Results[0];
        if (firstResult.IsPartial) {
            return [transcript]; // we don't split here because partials dont contain speaker information
        }

        transcript.Transcript.Results[0].Alternatives[0].Items.forEach(item => {
            if (item.Speaker) { // this is because punctuation does not have a speaker label.
                lastSpeaker = item.Speaker;
                if (initialSpeaker === undefined) {
                    initialSpeaker = item.Speaker;
                }
            }
            if (lastSpeaker) {
                if (!itemsBySpeaker[lastSpeaker]) {
                    itemsBySpeaker[lastSpeaker] = [];
                }
                itemsBySpeaker[lastSpeaker].push(item);
            }
        });
    }

    return Object.keys(itemsBySpeaker).map(speaker => {
        return {
            Transcript: {
                Results: [{
                    Alternatives: [{
                        Items: itemsBySpeaker[speaker],
                        Transcript: concatItemsIntoTranscript(itemsBySpeaker[speaker])
                    }],
                    ChannelId: firstResult?.ChannelId,
                    EndTime: itemsBySpeaker[speaker][itemsBySpeaker[speaker].length - 1].EndTime,
                    IsPartial: firstResult?.IsPartial,
                    ResultId: firstResult?.ResultId + (speaker === initialSpeaker ? '' : '-' + speaker),
                    StartTime: itemsBySpeaker[speaker][0].StartTime
                }]
            }
        };
    });
};
