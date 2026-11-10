/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Uploading a meeting's audio recording without replacing one already stored.
 *
 * A meeting's recording is stored as `<prefix><callId>.wav` (with the raw PCM
 * beside it as `.raw`). Most meetings are recorded by one session on this
 * task, but a client that reconnects to a different task, or after the
 * reconnect window (see calleventdata/detached-sessions.ts), records the rest
 * of the meeting as a second session under the same callId. Each upload is
 * therefore conditional on the name being free; a later session that finds the
 * meeting's recording already stored keeps its own audio under a name of its
 * own, `<callId>_<timestamp>.wav`, and the meeting keeps the recording it has.
 */
import fs from 'fs';
import { PutObjectCommand, S3Client } from '@aws-sdk/client-s3';

import { normalizeErrorForLogging } from './common';

export type PutOutcome = 'stored' | 'exists' | 'failed';

export interface RecordingStore {
    /** Store a local file at `key` unless an object is already there. */
    putIfAbsent(key: string, filePath: string): Promise<PutOutcome>;
}

interface Logger {
    info: (message: string) => void;
    warn: (message: string) => void;
    error: (message: string) => void;
}

/** S3 reports a taken name as 412 Precondition Failed, or 409 when two writes race. */
const isNameTaken = (error: unknown): boolean => {
    const err = error as { name?: string; Code?: string; $metadata?: { httpStatusCode?: number } };
    const status = err?.$metadata?.httpStatusCode;
    return (
        status === 412 ||
        status === 409 ||
        err?.name === 'PreconditionFailed' ||
        err?.Code === 'PreconditionFailed' ||
        err?.name === 'ConditionalRequestConflict'
    );
};

export const s3RecordingStore = (client: S3Client, bucket: string | undefined, log: Logger): RecordingStore => ({
    putIfAbsent: async (key: string, filePath: string): Promise<PutOutcome> => {
        const body = fs.createReadStream(filePath);
        try {
            await client.send(
                new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, IfNoneMatch: '*' })
            );
            return 'stored';
        } catch (error) {
            if (isNameTaken(error)) {
                return 'exists';
            }
            log.error(`[RECORDING]: Error uploading ${filePath} to ${key}: ${normalizeErrorForLogging(error)}`);
            return 'failed';
        } finally {
            body.destroy();
        }
    },
});

export interface RecordingUpload {
    /** Key of the stored WAV. */
    wavKey: string;
    outcome: PutOutcome;
    /**
     * True when the meeting already had a recording, so this one was stored
     * under a name of its own and the meeting's recording is left as it is.
     */
    storedSeparately: boolean;
}

/** `2026-11-03T10-15-30-123Z`, the stamp the muxed video recordings carry too. */
export const recordingStamp = (now: Date = new Date()): string => now.toISOString().replace(/[:.]/g, '-');

/**
 * Store a session's WAV and raw recording under the meeting's name, or under a
 * name of their own when the meeting's recording is already stored.
 */
export const uploadRecording = async (
    store: RecordingStore,
    options: { prefix: string; baseName: string; wavPath: string; rawPath: string; stamp?: string },
    log: Logger
): Promise<RecordingUpload> => {
    let name = options.baseName;
    let outcome = await store.putIfAbsent(`${options.prefix}${name}.wav`, options.wavPath);
    let storedSeparately = false;
    if (outcome === 'exists') {
        storedSeparately = true;
        name = `${options.baseName}_${options.stamp ?? recordingStamp()}`;
        log.warn(
            `[RECORDING]: ${options.prefix}${options.baseName}.wav is already stored for this meeting; ` +
                `storing this session's recording as ${options.prefix}${name}.wav instead of replacing it.`
        );
        outcome = await store.putIfAbsent(`${options.prefix}${name}.wav`, options.wavPath);
    }
    const raw = await store.putIfAbsent(`${options.prefix}${name}.raw`, options.rawPath);
    if (raw === 'exists') {
        log.warn(`[RECORDING]: ${options.prefix}${name}.raw is already stored; left as it is.`);
    }
    return { wavKey: `${options.prefix}${name}.wav`, outcome, storedSeparately };
};
