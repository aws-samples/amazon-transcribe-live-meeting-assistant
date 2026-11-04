/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { PutOutcome, RecordingStore, uploadRecording } from './recording-upload';

const quiet = { info: () => undefined, warn: () => undefined, error: () => undefined };

/** An in-memory bucket that refuses to replace an object, as a conditional put does. */
const bucket = (existing: string[] = []): RecordingStore & { keys: Map<string, string> } => {
    const keys = new Map<string, string>(existing.map((key) => [key, 'earlier']));
    return {
        keys,
        putIfAbsent: async (key: string, filePath: string): Promise<PutOutcome> => {
            if (keys.has(key)) {
                return 'exists';
            }
            keys.set(key, filePath);
            return 'stored';
        },
    };
};

const files = { prefix: 'lma-audio-recordings/', baseName: 'Meeting_1', wavPath: '/tmp/a.wav', rawPath: '/tmp/a.raw' };

test('the first recording of a meeting is stored under its name', async () => {
    const store = bucket();
    const upload = await uploadRecording(store, files, quiet);

    assert.deepEqual(upload, { wavKey: 'lma-audio-recordings/Meeting_1.wav', outcome: 'stored', storedSeparately: false });
    assert.equal(store.keys.get('lma-audio-recordings/Meeting_1.raw'), '/tmp/a.raw');
});

test('a recording already stored for the meeting is kept, and the later one stored beside it', async () => {
    const store = bucket(['lma-audio-recordings/Meeting_1.wav', 'lma-audio-recordings/Meeting_1.raw']);
    const upload = await uploadRecording(store, { ...files, stamp: '2026-11-03T10-00-00-000Z' }, quiet);

    assert.equal(store.keys.get('lma-audio-recordings/Meeting_1.wav'), 'earlier');
    assert.equal(store.keys.get('lma-audio-recordings/Meeting_1.raw'), 'earlier');
    assert.deepEqual(upload, {
        wavKey: 'lma-audio-recordings/Meeting_1_2026-11-03T10-00-00-000Z.wav',
        outcome: 'stored',
        storedSeparately: true,
    });
    assert.equal(store.keys.get('lma-audio-recordings/Meeting_1_2026-11-03T10-00-00-000Z.raw'), '/tmp/a.raw');
});

test('a failed upload is reported, not mistaken for an existing recording', async () => {
    const store: RecordingStore = { putIfAbsent: async () => 'failed' };
    const upload = await uploadRecording(store, files, quiet);

    assert.deepEqual(upload, { wavKey: 'lma-audio-recordings/Meeting_1.wav', outcome: 'failed', storedSeparately: false });
});
