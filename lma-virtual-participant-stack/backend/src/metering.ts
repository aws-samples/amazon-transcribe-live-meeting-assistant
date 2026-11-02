/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Metering records for the meeting cost estimate, written from the Virtual
 * Participant.
 *
 * One DynamoDB item per metered event, in the meeting table under
 * `PK = mtr#<CallId>`, in exactly the shape the AI stack's
 * `lma_metering.records.build_item` writes. The WebSocket transcriber carries
 * the same writer; `lma-ai-stack/test/test_metering_record_contract.py` keeps
 * the copies in step. (There is no shared TypeScript package in LMA, which is
 * why this is a copy rather than an import -- see docs/configuration-store.md.)
 *
 * What the Virtual Participant meters, all under its meeting's CallId:
 *
 *  - Its own compute: launch-type vCPU and memory multiplied by how long the
 *    process ran (Fargate, Lambda MicroVM), or the runtime alone on EC2, whose
 *    hosts are billed whether or not a meeting is running.
 *  - Amazon Transcribe streaming, per request, as the transcriber does, or the
 *    ASR MicroVM's lifetime when that engine transcribed the meeting.
 *  - Nova Sonic speech and text tokens, from the model's own usage events.
 *  - The Claude calls the page-automation fallback makes when a meeting
 *    platform's UI has changed.
 *
 * Writing never throws: a lost record makes the estimate lower than it was,
 * which docs/meeting-cost-estimate.md lists as a known limit.
 */

import { randomUUID } from 'node:crypto';
import { DynamoDBClient, PutItemCommand } from '@aws-sdk/client-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';
import { createLogger } from './logger.js';

const log = createLogger('metering');

export const PK_PREFIX = 'mtr#';
export const TABLE_ENV_VAR = 'METERING_TABLE_NAME';
export const EXPIRATION_ENV_VAR = 'METERING_EXPIRATION_DAYS';

export const CONTEXT_TRANSCRIPTION = 'Transcription';
export const CONTEXT_VIRTUAL_PARTICIPANT = 'VirtualParticipant';
export const CONTEXT_VOICE_ASSISTANT = 'VoiceAssistant';
export const CONTEXT_PAGE_AUTOMATION = 'PageAutomation';

export const SERVICE_TRANSCRIBE_STREAMING = 'transcribe/streaming';
export const SERVICE_TRANSCRIBE_PII_REDACTION = 'transcribe/streaming-pii-redaction';
export const SERVICE_MICROVM_ARM64 = 'lambda-microvm/arm64';
export const SERVICE_FARGATE_X86_64 = 'fargate/x86_64';
export const SERVICE_FARGATE_ARM64 = 'fargate/arm64';
export const SERVICE_EC2_HOST = 'ec2/virtual-participant-host';

export const TRANSCRIBE_MINIMUM_SECONDS_PER_REQUEST = 15;
/** Fargate bills at least one minute per task. */
export const FARGATE_MINIMUM_SECONDS = 60;
/** MicroVMs are sized at 2 GiB of memory per vCPU. */
export const MICROVM_MIB_PER_VCPU = 2048;

export type Usage = Record<string, number>;
export type Details = Record<string, string | number | boolean | undefined>;

export const transcribeBilledSeconds = (audioSeconds: number): number =>
    Math.max(TRANSCRIBE_MINIMUM_SECONDS_PER_REQUEST, Math.ceil(Math.max(0, audioSeconds)));

export const pcmSeconds = (bytes: number, sampleRate: number, channels: number): number =>
    sampleRate && channels ? bytes / (sampleRate * 2 * channels) : 0;

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

export const buildMeteringItem = (
    callId: string,
    context: string,
    service: string,
    usage: Usage,
    options: BuildOptions,
): Record<string, AttributeValue> | undefined => {
    if (!callId || !context || !service) {
        return undefined;
    }
    const clean = Object.entries(usage || {}).filter(
        ([, value]) => typeof value === 'number' && Number.isFinite(value) && value >= 0,
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
        item.Details = { M: detailMap };
    }
    if (options.region) {
        item.Region = { S: options.region };
    }
    const days = options.expirationDays ?? expirationDays();
    if (days !== undefined) {
        item.ExpiresAfter = { N: String(Math.floor(now.getTime() / 1000 + days * 86400)) };
    }
    return item;
};

let client: { send: DynamoDBClient['send'] } | undefined;

/** Replaceable for tests. */
export const setMeteringClient = (replacement: { send: DynamoDBClient['send'] } | undefined): void => {
    client = replacement;
};

/** The meeting this Virtual Participant is in, once it has joined. */
export const currentCallId = (): string => process.env.VP_CALL_ID || '';

/**
 * Store one record. Resolves to whether it was stored; never rejects.
 */
export const recordMetering = async (
    callId: string,
    context: string,
    service: string,
    usage: Usage,
    details?: Details,
    region?: string,
): Promise<boolean> => {
    const table = process.env[TABLE_ENV_VAR] || '';
    if (!table) {
        return false;
    }
    const item = buildMeteringItem(callId, context, service, usage, {
        source: 'virtual-participant',
        details,
        region,
    });
    if (!item) {
        return false;
    }
    try {
        if (!client) {
            client = new DynamoDBClient({
                region: process.env.VP_AWS_REGION || process.env.AWS_REGION || 'us-east-1',
            });
        }
        await client.send(new PutItemCommand({ TableName: table, Item: item }));
        return true;
    } catch (error) {
        log.warn(
            `[METERING] could not write the ${service} record for ${callId}: ${
                error instanceof Error ? error.message : String(error)
            }`,
        );
        return false;
    }
};

/** One Amazon Transcribe streaming request, plus the redaction add-on when applied. */
export const meterTranscribeRequest = async (
    callId: string,
    audioSeconds: number,
    options: { redaction: boolean; channels: number },
): Promise<void> => {
    const seconds = transcribeBilledSeconds(audioSeconds);
    const details: Details = {
        engine: 'amazon-transcribe',
        channels: options.channels,
        audioSeconds: Math.round(audioSeconds * 10) / 10,
        minimum: `${TRANSCRIBE_MINIMUM_SECONDS_PER_REQUEST}s per request`,
    };
    const writes = [
        recordMetering(callId, CONTEXT_TRANSCRIPTION, SERVICE_TRANSCRIBE_STREAMING, { seconds, requests: 1 }, details),
    ];
    if (options.redaction) {
        writes.push(
            recordMetering(callId, CONTEXT_TRANSCRIPTION, SERVICE_TRANSCRIBE_PII_REDACTION, { seconds }, details),
        );
    }
    await Promise.all(writes);
};

/** One ASR MicroVM's lifetime, at its configured size. */
export const meterAsrMicrovm = async (
    callId: string,
    runSeconds: number,
    memoryMiB: number,
    details: Details,
): Promise<boolean> => {
    if (!(runSeconds > 0) || !(memoryMiB > 0)) {
        return false;
    }
    const vcpus = memoryMiB / MICROVM_MIB_PER_VCPU;
    return recordMetering(
        callId,
        CONTEXT_TRANSCRIPTION,
        SERVICE_MICROVM_ARM64,
        { vcpuSeconds: runSeconds * vcpus, gbSeconds: (runSeconds * memoryMiB) / 1024, runtimeSeconds: runSeconds },
        { engine: 'microvm-asr', memoryMiB, vcpus, ...details },
    );
};

export type LaunchType = 'EC2' | 'FARGATE' | 'MICROVM';

export interface ComputeSize {
    vcpus: number;
    memoryMiB: number;
    architecture: 'x86_64' | 'arm64';
    /** Where the size came from, shown with the line. */
    sizeSource: string;
}

/** The Node architecture name in the price table's terms. */
export const priceArchitecture = (arch: string = process.arch): 'x86_64' | 'arm64' =>
    arch === 'arm64' ? 'arm64' : 'x86_64';

/**
 * The service and usage for a Virtual Participant that ran for `runSeconds`.
 * Pure, so the arithmetic is tested without a meeting.
 */
export const virtualParticipantUsage = (
    launchType: LaunchType,
    runSeconds: number,
    size: ComputeSize,
): { service: string; usage: Usage } => {
    if (launchType === 'EC2') {
        // The host is billed by the hour whether or not this meeting ran on it,
        // so no share of it is attributed here; the runtime is shown for reference.
        return { service: SERVICE_EC2_HOST, usage: { runtimeSeconds: runSeconds } };
    }
    const billedSeconds = launchType === 'FARGATE' ? Math.max(FARGATE_MINIMUM_SECONDS, runSeconds) : runSeconds;
    const service =
        launchType === 'MICROVM'
            ? SERVICE_MICROVM_ARM64
            : size.architecture === 'arm64'
              ? SERVICE_FARGATE_ARM64
              : SERVICE_FARGATE_X86_64;
    return {
        service,
        usage: {
            vcpuSeconds: billedSeconds * size.vcpus,
            gbSeconds: (billedSeconds * size.memoryMiB) / 1024,
            runtimeSeconds: runSeconds,
        },
    };
};

/**
 * The size of the compute this process is running on.
 *
 * Fargate reports its task limits through the ECS task metadata endpoint, which
 * is read when available. A MicroVM's size is its image's configured memory;
 * the image environment cannot carry it without a rebuild, so the default here
 * is the template's `MinimumMemoryInMiB` and `VP_MICROVM_MEMORY_MIB` overrides it.
 */
export const resolveComputeSize = async (
    launchType: LaunchType,
    fetcher: typeof fetch = fetch,
): Promise<ComputeSize> => {
    if (launchType === 'MICROVM') {
        const memoryMiB = Number(process.env.VP_MICROVM_MEMORY_MIB) > 0 ? Number(process.env.VP_MICROVM_MEMORY_MIB) : 4096;
        return {
            vcpus: memoryMiB / MICROVM_MIB_PER_VCPU,
            memoryMiB,
            architecture: 'arm64',
            sizeSource: process.env.VP_MICROVM_MEMORY_MIB ? 'VP_MICROVM_MEMORY_MIB' : 'image default',
        };
    }
    const fallback: ComputeSize = {
        vcpus: 2,
        memoryMiB: 4096,
        architecture: priceArchitecture(),
        sizeSource: 'task definition default',
    };
    const metadataUri = process.env.ECS_CONTAINER_METADATA_URI_V4;
    if (!metadataUri) {
        return fallback;
    }
    try {
        const response = await fetcher(`${metadataUri}/task`);
        const task = (await response.json()) as { Limits?: { CPU?: number; Memory?: number } };
        const vcpus = Number(task.Limits?.CPU);
        const memoryMiB = Number(task.Limits?.Memory);
        if (vcpus > 0 && memoryMiB > 0) {
            return { vcpus, memoryMiB, architecture: priceArchitecture(), sizeSource: 'ECS task metadata' };
        }
    } catch (error) {
        log.warn(`[METERING] could not read the task size: ${error instanceof Error ? error.message : error}`);
    }
    return fallback;
};

/**
 * Nova Sonic token usage, accumulated from the model's `usageEvent` deltas and
 * written as one record per flush. Flushed periodically and at shutdown, so a
 * long meeting's usage is on record before it ends.
 */
export class NovaSonicUsage {
    private pending: Usage = {};
    private requests = 0;

    constructor(
        private readonly modelId: string,
        private readonly region?: string,
    ) {}

    /** Add one usageEvent's delta. Tolerates a missing or partial event. */
    add(usageEvent: unknown): void {
        const delta = (usageEvent as { details?: { delta?: Record<string, Record<string, unknown>> } })?.details
            ?.delta;
        if (!delta) {
            return;
        }
        const add = (unit: string, value: unknown): void => {
            if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
                this.pending[unit] = (this.pending[unit] || 0) + value;
            }
        };
        add('speechInputTokens', delta.input?.speechTokens);
        add('textInputTokens', delta.input?.textTokens);
        add('speechOutputTokens', delta.output?.speechTokens);
        add('textOutputTokens', delta.output?.textTokens);
    }

    /** Count one model session (one bidirectional stream). */
    noteSession(): void {
        this.requests += 1;
    }

    snapshot(): Usage {
        return { ...this.pending, ...(this.requests ? { sessions: this.requests } : {}) };
    }

    async flush(callId: string = currentCallId()): Promise<boolean> {
        const usage = this.snapshot();
        if (Object.keys(usage).length === 0 || !callId) {
            return false;
        }
        this.pending = {};
        this.requests = 0;
        return recordMetering(callId, CONTEXT_VOICE_ASSISTANT, `bedrock/${this.modelId}`, usage, {}, this.region);
    }
}

/** The billable units of an Anthropic-native InvokeModel response's usage block. */
export const anthropicUsage = (usage: unknown): Usage => {
    const u = (usage || {}) as Record<string, unknown>;
    const out: Usage = { requests: 1 };
    const take = (unit: string, value: unknown): void => {
        if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
            out[unit] = value;
        }
    };
    take('inputTokens', u.input_tokens);
    take('outputTokens', u.output_tokens);
    take('cacheReadInputTokens', u.cache_read_input_tokens);
    take('cacheWriteInputTokens', u.cache_creation_input_tokens);
    return out;
};
