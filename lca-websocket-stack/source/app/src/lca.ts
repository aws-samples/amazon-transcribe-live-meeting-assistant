// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

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
    ContentRedactionType
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
    Uuid
} from './entities-lca';

import stream from 'stream';

const formatPath = function(path:string) {
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

const tcaOutputLocation = `s3://${RECORDINGS_BUCKET_NAME}/${CALL_ANALYTICS_FILE_PREFIX}`;

type transcriptionCommandInput<TCAEnabled> = TCAEnabled extends true 
    ? StartCallAnalyticsStreamTranscriptionCommandInput
    : StartStreamTranscriptionCommandInput;
  
export type CallMetaData = {
    callId: Uuid,
    fromNumber?: string,
    toNumber?: string,
    shouldRecordCall?: boolean,
    agentId?: string,
    samplingRate: number,
    callEvent: string,
};

const kinesisClient = new KinesisClient({ region: AWS_REGION });
const transcribeClient = new TranscribeStreamingClient({ region: AWS_REGION });

export const writeCallEvent = async (callEvent: CallStartEvent | CallEndEvent | CallRecordingEvent ) => {
    
    const putParams = {
        StreamName: kdsStreamName,
        PartitionKey: callEvent.CallId,
        Data: Buffer.from(JSON.stringify(callEvent))
    };

    const putCmd = new PutRecordCommand(putParams);
    try {
        await kinesisClient.send(putCmd);
        console.debug('Written Call Event to KDS');
        console.debug(callEvent);
    } catch (error) {
        console.error('Error writing Call Event to KDS', error);
        console.debug(callEvent);
    }
};

export const writeTranscriptionSegment = async function(transcribeMessageJson:TranscriptEvent, callId: Uuid) {
    if (transcribeMessageJson.Transcript?.Results && transcribeMessageJson.Transcript?.Results.length > 0) {
        if (transcribeMessageJson.Transcript?.Results[0].Alternatives && transcribeMessageJson.Transcript?.Results[0].Alternatives?.length > 0) {

            const result = transcribeMessageJson.Transcript?.Results[0];

            if (result.IsPartial == undefined || (result.IsPartial == true && !savePartial)) {
                return;
            }
            const { Transcript: transcript } = transcribeMessageJson.Transcript.Results[0].Alternatives[0];
            const now = new Date().toISOString();

            const kdsObject:AddTranscriptSegmentEvent = {
                EventType: 'ADD_TRANSCRIPT_SEGMENT',
                CallId: callId,
                Channel: (result.ChannelId ==='ch_0' ? 'CALLER' : 'AGENT'),
                SegmentId: result.ResultId || '',
                StartTime: result.StartTime || 0,
                EndTime: result.EndTime || 0,
                Transcript: transcript || '',
                IsPartial: result.IsPartial,
                CreatedAt: now,
                UpdatedAt: now,
                Sentiment: undefined,
                TranscriptEvent: undefined,
                UtteranceEvent: undefined,
            };

            const putParams = {
                StreamName: kdsStreamName,
                PartitionKey: callId,
                Data: Buffer.from(JSON.stringify(kdsObject)),
            };

            const putCmd = new PutRecordCommand(putParams);
            try {
                await kinesisClient.send(putCmd);
                // console.info('Written ADD_TRANSCRIPT_SEGMENT event to KDS');
                // console.info(JSON.stringify(kdsObject));
                // console.info(kdsObject.Transcript);
            } catch (error) {
                console.error('Error writing transcription segment (TRANSCRIBE) to KDS', error);
                console.debug(JSON.stringify(kdsObject));
            }
        } 
    } 
};

export const writeAddTranscriptSegmentEvent = async function(utteranceEvent:UtteranceEvent | undefined , 
    transcriptEvent:TranscriptEvent | undefined,  callId: Uuid) {
    
    if (transcriptEvent) {
        if (transcriptEvent.Transcript?.Results && transcriptEvent.Transcript?.Results.length > 0) {
            if (transcriptEvent.Transcript?.Results[0].Alternatives && transcriptEvent.Transcript?.Results[0].Alternatives?.length > 0) {
            
                const result = transcriptEvent.Transcript?.Results[0];
                if (result.IsPartial == undefined || (result.IsPartial == true && !savePartial)) {
                    return;
                }
            }
        }
    }
                
    if (utteranceEvent) {
        if (utteranceEvent.IsPartial == undefined || (utteranceEvent.IsPartial == true && !savePartial)) {
            return;
        }
    }
   
    const now = new Date().toISOString();

    const kdsObject:AddTranscriptSegmentEvent = {
        EventType: 'ADD_TRANSCRIPT_SEGMENT',
        CallId: callId,
        TranscriptEvent: transcriptEvent,
        UtteranceEvent: utteranceEvent,
        CreatedAt: now,
        UpdatedAt: now,
    };

    const putParams = {
        StreamName: kdsStreamName,
        PartitionKey: callId,
        Data: Buffer.from(JSON.stringify(kdsObject)),
    };

    const putCmd = new PutRecordCommand(putParams);
    try {
        await kinesisClient.send(putCmd);
        // console.info('Written ADD_TRANSCRIPT_SEGMENT event to KDS');
        // console.info(JSON.stringify(kdsObject));
    } catch (error) {
        console.error('Error writing transcription segment to KDS', error);
        console.debug(JSON.stringify(kdsObject));
    }
};

export const writeAddCallCategoryEvent = async function(categoryEvent:CategoryEvent, callId: Uuid) {

    if (categoryEvent) {
        const now = new Date().toISOString();
    
        const kdsObject:AddCallCategoryEvent = {
            EventType: 'ADD_CALL_CATEGORY',
            CallId: callId,
            CategoryEvent: categoryEvent,
            CreatedAt: now,
        };

        const putParams = {
            StreamName: kdsStreamName,
            PartitionKey: callId,
            Data: Buffer.from(JSON.stringify(kdsObject)),
        };

        const putCmd = new PutRecordCommand(putParams);
        try {
            await kinesisClient.send(putCmd);
            // console.debug('Written ADD_CALL_CATEGORY to KDS');
            // console.debug(JSON.stringify(kdsObject));
        } catch (error) {
            console.error('Error writing ADD_CALL_CATEGORY to KDS', error);
            console.debug(JSON.stringify(kdsObject));
        }

    }
};

export const writeCallStartEvent = async (callMetaData: CallMetaData): Promise<void> => {
    const callStartEvent: CallStartEvent = {
        EventType: 'START',
        CallId: callMetaData.callId,
        CustomerPhoneNumber: callMetaData.fromNumber || 'Customer Phone',
        SystemPhoneNumber: callMetaData.toNumber || 'System Phone',
        AgentId: callMetaData.agentId,
        CreatedAt: new Date().toISOString()
    };
    await writeCallEvent(callStartEvent);  
};

export const writeCallEndEvent = async (callMetaData: CallMetaData): Promise<void> => {
    const callEndEvent: CallEndEvent = {
        EventType: 'END',
        CallId: callMetaData.callId,
        CustomerPhoneNumber: callMetaData.fromNumber || 'Customer Phone',
        SystemPhoneNumber: callMetaData.toNumber || 'System Phone',
    };
    await writeCallEvent(callEndEvent);  
};

export const startTranscribe = async (callMetaData: CallMetaData, audioInputStream: stream.PassThrough) => {

    const transcribeInput = async function* () {
        if (isTCAEnabled) {
            const channel0: ChannelDefinition = { ChannelId:0, ParticipantRole: ParticipantRole.CUSTOMER };
            const channel1: ChannelDefinition = { ChannelId:1, ParticipantRole: ParticipantRole.AGENT };
            const channel_definitions: ChannelDefinition [] = [];
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
        for await (const chunk of audioInputStream ) {
            yield { AudioEvent: { AudioChunk: chunk } };
        }
        // yield { AudioEvent: { AudioChunk:Uint8Array.from(new Array(2).fill([0x00, 0x00]).flat()), EndOfStream: true } };

    };

    let tsStream;
    let outputCallAnalyticsStream: AsyncIterable<CallAnalyticsTranscriptResultStream> | undefined;
    let outputTranscriptStream: AsyncIterable<TranscriptResultStream> | undefined;
    
    const tsParams:transcriptionCommandInput<typeof isTCAEnabled> = {
        LanguageCode: TRANSCRIBE_LANGUAGE_CODE as LanguageCode,
        MediaSampleRateHertz: callMetaData.samplingRate,
        MediaEncoding: 'pcm',
        AudioStream: transcribeInput()
    };
    
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
        const response = await transcribeClient.send(
            new StartCallAnalyticsStreamTranscriptionCommand(tsParams as StartCallAnalyticsStreamTranscriptionCommandInput)
        );
        console.log(
            `=== Received Initial response from TCA. Session Id: ${response.SessionId} ===`
        );
        outputCallAnalyticsStream = response.CallAnalyticsTranscriptResultStream;
    } else {
        (tsParams as StartStreamTranscriptionCommandInput).EnableChannelIdentification = true;
        (tsParams as StartStreamTranscriptionCommandInput).NumberOfChannels = 2;
        const response = await transcribeClient.send(
            new StartStreamTranscriptionCommand(tsParams)
        );
        console.log(
            `=== Received Initial response from Transcribe. Session Id: ${response.SessionId} ===`
        );
        outputTranscriptStream = response.TranscriptResultStream;
    }
    
    if (outputCallAnalyticsStream) {
        tsStream = stream.Readable.from(outputCallAnalyticsStream);
    } else if (outputTranscriptStream) {
        tsStream = stream.Readable.from(outputTranscriptStream);
    }

    try {
        if (tsStream) {
            for await (const event of tsStream) {
                // console.log('Event ', event);
                if (event.TranscriptEvent) {
                    const message: TranscriptEvent = event.TranscriptEvent;
                    await writeTranscriptionSegment(message, callMetaData.callId);
                }
                if (event.CategoryEvent && event.CategoryEvent.MatchedCategories) {
                    await writeAddCallCategoryEvent(event.CategoryEvent, callMetaData.callId);
                }
                if (event.UtteranceEvent && event.UtteranceEvent.UtteranceId) {
                    await writeAddTranscriptSegmentEvent(event.UtteranceEvent, undefined, callMetaData.callId);
                }
            }

        } else {
            console.log('Transcribe stream is empty');
        }
    } catch (error) {
        console.log('Error processing Transcribe results stream', error);
        
    } finally {
        writeCallEndEvent(callMetaData);
    }
};