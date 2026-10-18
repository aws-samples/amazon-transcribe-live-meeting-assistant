/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Ask which enrolled voices a starting meeting may label by name.
 *
 * The answer comes from a Lambda in the AI stack rather than from a table read
 * here, and that is deliberate on two counts. Whether an enrollment may be used
 * turns on four promises made to the person who enrolled, and those rules live
 * in one implementation instead of being restated in a second language. And the
 * voiceprints themselves are read by one role, which this task does not hold:
 * they reach it only as the answer to this call.
 *
 * Nothing is cached. A withdrawal is promised to take effect for every later
 * meeting, and a cache is exactly a window in which it would not -- one call per
 * meeting start is a small price for that promise being true. It also means no
 * biometric data outlives the session it was fetched for.
 */
import { InvokeCommand, LambdaClient } from '@aws-sdk/client-lambda';
import { FastifyInstance } from 'fastify';

import { normalizeErrorForLogging } from '../utils/common';
import type { EnrolledVoice } from './asr-microvm';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';

/**
 * Empty unless the deployment has voice enrollment switched on, in which case
 * no call is made at all and every meeting behaves exactly as it did before.
 *
 * Read per call rather than once at import, so switching enrollment on or off
 * is a question about the environment now and not about when this module
 * happened to load.
 */
const seedsFunctionArn = (): string => process.env['VOICE_ENROLLMENT_SEEDS_FUNCTION_ARN'] || '';

/**
 * How long a meeting waits for the answer.
 *
 * The opening seconds of audio are buffered while this is in flight, so a slow
 * reply costs transcript latency. Names are an enhancement and a transcript is
 * not, so the wait is bounded and giving up means the meeting starts with voice
 * ids — which a person, or the transcript miner, can name afterwards.
 */
const SEEDS_TIMEOUT_MS = parseInt(process.env['VOICE_ENROLLMENT_SEEDS_TIMEOUT_MS'] || '5000', 10);

const lambdaClient = new LambdaClient({ region: AWS_REGION });

export const voiceEnrollmentEnabled = (): boolean => seedsFunctionArn() !== '';

interface SeedsResponse {
    EnrolledVoices?: Array<{ label?: unknown; vector?: unknown }>;
}

/**
 * Keep only what the engine can use, and nothing that merely looks like it.
 *
 * The response crosses a stack boundary, so it is treated as input rather than
 * trusted: a row that is not a label and a list of finite numbers is dropped
 * here, and `enrolledVoicesForConfig` applies the dimension and count bounds
 * before anything goes on the wire.
 */
const parseVoices = (body: SeedsResponse | undefined): EnrolledVoice[] => {
    const rows = Array.isArray(body?.EnrolledVoices) ? body.EnrolledVoices : [];
    const voices: EnrolledVoice[] = [];
    for (const row of rows) {
        if (typeof row?.label !== 'string' || !Array.isArray(row.vector)) {
            continue;
        }
        const vector = row.vector.filter((value): value is number => typeof value === 'number');
        if (vector.length !== row.vector.length) {
            // A partly numeric vector is a different thing from a short one:
            // silently dropping components would leave a vector that compares
            // against the enrolled centroids and matches the wrong person.
            continue;
        }
        voices.push({ label: row.label, vector });
    }
    return voices;
};

/**
 * The enrolled voices this meeting may label by name, or an empty list.
 *
 * Never rejects. Every failure path — enrollment off, a slow or erroring
 * function, a malformed answer — produces no names rather than a failed
 * meeting.
 */
export const fetchEnrolledVoices = async (
    callId: string,
    server: FastifyInstance
): Promise<EnrolledVoice[]> => {
    if (!voiceEnrollmentEnabled()) {
        return [];
    }
    const started = Date.now();
    try {
        const response = await lambdaClient.send(
            new InvokeCommand({
                FunctionName: seedsFunctionArn(),
                Payload: Buffer.from(JSON.stringify({ CallId: callId })),
            }),
            { requestTimeout: SEEDS_TIMEOUT_MS }
        );
        if (response.FunctionError) {
            server.log.error(
                `[ASR]: [${callId}] - enrolled voices lookup failed: ${response.FunctionError}`
            );
            return [];
        }
        const body = response.Payload
            ? (JSON.parse(Buffer.from(response.Payload).toString('utf8')) as SeedsResponse)
            : undefined;
        const voices = parseVoices(body);
        // Counts only. The names and the voiceprints are the payload of this
        // call and neither belongs in a log line.
        server.log.info(
            `[ASR]: [${callId}] - ${voices.length} enrolled voice(s) available in ${
                Date.now() - started
            }ms`
        );
        return voices;
    } catch (error) {
        server.log.error(
            `[ASR]: [${callId}] - enrolled voices lookup error: ${normalizeErrorForLogging(error)}`
        );
        return [];
    }
};

export const seedsTimeoutMs = (): number => SEEDS_TIMEOUT_MS;
