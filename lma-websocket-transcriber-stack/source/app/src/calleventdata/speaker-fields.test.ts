/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { applySpeakerFieldAliases, SpeakerFields } from './speaker-fields';

test('the documented speaker names are read as the microphone and meeting-audio labels', () => {
    const frame = applySpeakerFieldAliases<SpeakerFields>({
        localSpeakerName: 'ana@example.com',
        remoteSpeakerName: 'Room 3',
    });
    assert.equal(frame.agentId, 'ana@example.com');
    assert.equal(frame.fromNumber, 'Room 3');
});

test('a frame with only the older names is unchanged', () => {
    const frame = applySpeakerFieldAliases({ agentId: 'ana@example.com', fromNumber: 'Room 3' });
    assert.deepEqual(frame, { agentId: 'ana@example.com', fromNumber: 'Room 3' });
});

test('the documented name wins when a frame carries both spellings', () => {
    const frame = applySpeakerFieldAliases({
        agentId: 'old-local',
        localSpeakerName: 'new-local',
        fromNumber: 'old-remote',
        remoteSpeakerName: 'new-remote',
    });
    assert.equal(frame.agentId, 'new-local');
    assert.equal(frame.fromNumber, 'new-remote');
});

test('an empty or non-string documented name leaves the older field as sent', () => {
    const frame = applySpeakerFieldAliases({
        agentId: 'ana@example.com',
        localSpeakerName: '',
        fromNumber: 'Room 3',
        remoteSpeakerName: 42 as unknown as string,
    } as SpeakerFields);
    assert.equal(frame.agentId, 'ana@example.com');
    assert.equal(frame.fromNumber, 'Room 3');
});

test('a frame with neither spelling gets no label', () => {
    const frame = applySpeakerFieldAliases({} as SpeakerFields);
    assert.equal(frame.agentId, undefined);
    assert.equal(frame.fromNumber, undefined);
});
