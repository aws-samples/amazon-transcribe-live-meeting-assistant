/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Metering records for the meeting cost estimate, written from the transcriber.
 *
 * One DynamoDB item per metered event, in the meeting table under
 * `PK = mtr#<CallId>`, in exactly the shape the AI stack's
 * `lma_metering.records.build_item` writes -- see that module for the attribute
 * list, and `lma-ai-stack/test/test_metering_record_contract.py`, which keeps
 * this writer and the Virtual Participant's in step with it.
 *
 * What the transcriber meters:
 *
 *  - Amazon Transcribe streaming, per StartStreamTranscription request: the
 *    seconds of audio sent, rounded up, with the service's 15-second minimum per
 *    request. A two-channel stream with channel identification is billed at the
 *    stream's duration, not twice it, so the stereo byte count is converted at
 *    two channels. Content redaction and a custom language model are add-ons
 *    billed per second on top, and are recorded as their own lines.
 *  - An ASR MicroVM, when that engine served the meeting: its configured vCPU and
 *    memory multiplied by the time from launch to release.
 *
 * Writing never throws. A lost record makes a meeting's estimate lower than it
 * was, which docs/meeting-cost-estimate.md lists as a known limit; a transcript interrupted by a
 * metering write would be far worse.
 */

import { randomUUID } from 'node:crypto';
import { DynamoDBClient, PutItemCommand } from '@aws-sdk/client-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';

export const PK_PREFIX = 'mtr#';
export const TABLE_ENV_VAR = 'METERING_TABLE_NAME';
export const EXPIRATION_ENV_VAR = 'METERING_EXPIRATION_DAYS';

export const CONTEXT_TRANSCRIPTION = 'Transcription';

export const SERVICE_TRANSCRIBE_STREAMING = 'transcribe/streaming';
export const SERVICE_TRANSCRIBE_CALL_ANALYTICS = 'transcribe/streaming-call-analytics';
export const SERVICE_TRANSCRIBE_PII_REDACTION = 'transcribe/streaming-pii-redaction';
export const SERVICE_TRANSCRIBE_CUSTOM_LANGUAGE_MODEL = 'transcribe/streaming-custom-language-model';
export const SERVICE_MICROVM_ARM64 = 'lambda-microvm/arm64';

/** Amazon Transcribe streaming bills at least this many seconds per request. */
export const TRANSCRIBE_MINIMUM_SECONDS_PER_REQUEST = 15;

export type Usage = Record<string, number>;
export type Details = Record<string, string | number | boolean | undefined>;

/**
 * Billable seconds for one streaming request: whole seconds, never fewer than
 * the per-request minimum. Zero when no request was made.
 */
export const transcribeBilledSeconds = (audioSeconds: number): number =>
    Math.max(TRANSCRIBE_MINIMUM_SECONDS_PER_REQUEST, Math.ceil(Math.max(0, audioSeconds)));

/** Seconds of audio in `bytes` of 16-bit PCM at `sampleRate` Hz over `channels` channels. */
export const pcmSeconds = (bytes: number, sampleRate: number, channels: number): number => {
    if (!sampleRate || !channels) {
        return 0;
    }
    return bytes / (sampleRate * 2 * channels);
};

/** A DynamoDB number, rounded to 6 places and never in exponent form. */
const toNumber = (value: number): AttributeValue => {
    const rounded = Math.round(value * 1e6) / 1e6;
    let text = rounded.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
    if (text === '-0') {
        text = '0';
    }
    return { N: text };
};

const expirationDays = (): number | undefined => {
    const raw = (process.env[EXPIRATION_ENV_VAR] || '').trim();
    if (!raw) {
        return undefined;
    }
    const days = Number(raw);
    return Number.isFinite(days) && days > 0 ? days : undefined;
};

export interface BuildOptions {
    source: string;
    details?: Details;
    region?: string;
    expirationDays?: number;
    recordId?: string;
    now?: Date;
}

/**
 * The low-level DynamoDB item for one record, or undefined when the record would
 * say nothing (no meeting, or no numeric usage).
 */
export const buildMeteringItem = (
    callId: string,
    context: string,
    service: string,
    usage: Usage,
    options: BuildOptions
): Record<string, AttributeValue> | undefined => {
    if (!callId || !context || !service) {
        return undefined;
    }
    const clean = Object.entries(usage || {}).filter(
        ([, value]) => typeof value === 'number' && Number.isFinite(value) && value >= 0
    );
    if (clean.length === 0) {
        return undefined;
    }
    const now = options.now ?? new Date();
    const recordedAt = now.toISOString();
    const usageMap: Record<string, AttributeValue> = {};
    for (const [unit, value] of clean) {
        usageMap[unit] = toNumber(value);
    }
    const item: Record<string, AttributeValue> = {
        PK: { S: `${PK_PREFIX}${callId}` },
        SK: { S: `${context}#${recordedAt}#${options.recordId ?? randomUUID().replace(/-/g, '')}` },
        CallId: { S: callId },
        Context: { S: context },
        Service: { S: service },
        Usage: { M: usageMap },
        Source: { S: options.source },
        RecordedAt: { S: recordedAt },
    };
    const details = Object.entries(options.details || {}).filter(([, value]) => value !== undefined);
    if (details.length > 0) {
        const detailMap: Record<string, AttributeValue> = {};
        for (const [key, value] of details) {
            detailMap[key] = { S: String(value) };
        }
        item['Details'] = { M: detailMap };
    }
    if (options.region) {
        item['Region'] = { S: options.region };
    }
    const days = options.expirationDays ?? expirationDays();
    if (days !== undefined) {
        item['ExpiresAfter'] = { N: String(Math.floor(now.getTime() / 1000 + days * 86400)) };
    }
    return item;
};

let client: DynamoDBClient | undefined;

/** Replaceable for tests. */
export const setMeteringClient = (replacement: { send: DynamoDBClient['send'] } | undefined): void => {
    client = replacement as DynamoDBClient | undefined;
};

export interface Logger {
    warn: (message: string) => void;
}

/**
 * Store one record. Resolves to whether it was stored; never rejects.
 */
export const recordMetering = async (
    callId: string,
    context: string,
    service: string,
    usage: Usage,
    details?: Details,
    logger?: Logger
): Promise<boolean> => {
    const table = process.env[TABLE_ENV_VAR] || '';
    if (!table) {
        return false;
    }
    const item = buildMeteringItem(callId, context, service, usage, {
        source: 'websocket-transcriber',
        details,
    });
    if (!item) {
        return false;
    }
    try {
        if (!client) {
            client = new DynamoDBClient({ region: process.env['AWS_REGION'] || 'us-east-1' });
        }
        await client.send(new PutItemCommand({ TableName: table, Item: item }));
        return true;
    } catch (error) {
        logger?.warn(
            `[METERING]: [${callId}] - could not write the ${service} record: ${
                error instanceof Error ? error.message : String(error)
            }`
        );
        return false;
    }
};

/**
 * The records for one Amazon Transcribe streaming request.
 *
 * `addOns` names the per-second add-ons the request carried; each is its own
 * record because each has its own price.
 */
export const meterTranscribeRequest = async (
    callId: string,
    audioSeconds: number,
    options: {
        callAnalytics: boolean;
        redaction: boolean;
        customLanguageModel: boolean;
        channels: number;
        logger?: Logger;
    }
): Promise<void> => {
    const seconds = transcribeBilledSeconds(audioSeconds);
    const details: Details = {
        engine: 'amazon-transcribe',
        channels: options.channels,
        audioSeconds: Math.round(audioSeconds * 10) / 10,
        minimum: `${TRANSCRIBE_MINIMUM_SECONDS_PER_REQUEST}s per request`,
    };
    const base = options.callAnalytics ? SERVICE_TRANSCRIBE_CALL_ANALYTICS : SERVICE_TRANSCRIBE_STREAMING;
    const writes = [
        recordMetering(callId, CONTEXT_TRANSCRIPTION, base, { seconds, requests: 1 }, details, options.logger),
    ];
    if (options.redaction) {
        writes.push(
            recordMetering(
                callId,
                CONTEXT_TRANSCRIPTION,
                SERVICE_TRANSCRIBE_PII_REDACTION,
                { seconds },
                details,
                options.logger
            )
        );
    }
    if (options.customLanguageModel) {
        writes.push(
            recordMetering(
                callId,
                CONTEXT_TRANSCRIPTION,
                SERVICE_TRANSCRIBE_CUSTOM_LANGUAGE_MODEL,
                { seconds },
                details,
                options.logger
            )
        );
    }
    await Promise.all(writes);
};

/** MicroVMs are sized at 2 GiB of memory per vCPU. */
export const MICROVM_MIB_PER_VCPU = 2048;

/** The record for one ASR MicroVM's lifetime. */
export const meterMicrovm = async (
    callId: string,
    runSeconds: number,
    memoryMiB: number,
    details: Details,
    logger?: Logger
): Promise<boolean> => {
    if (!(runSeconds > 0) || !(memoryMiB > 0)) {
        return false;
    }
    const vcpus = memoryMiB / MICROVM_MIB_PER_VCPU;
    const gb = memoryMiB / 1024;
    return recordMetering(
        callId,
        CONTEXT_TRANSCRIPTION,
        SERVICE_MICROVM_ARM64,
        {
            vcpuSeconds: runSeconds * vcpus,
            gbSeconds: runSeconds * gb,
            runtimeSeconds: runSeconds,
        },
        { engine: 'microvm-asr', memoryMiB, vcpus, ...details },
        logger
    );
};
