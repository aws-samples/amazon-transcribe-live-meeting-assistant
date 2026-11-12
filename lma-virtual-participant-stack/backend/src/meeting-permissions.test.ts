/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the camera/microphone grant made for every join (GitHub #775).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { grantMeetingMediaPermissions, MEETING_MEDIA_ORIGINS } from './meeting-permissions.js';

test('grants camera and microphone to every meeting origin', async () => {
    const calls: { permissions: string[]; origin?: string }[] = [];
    const granted = await grantMeetingMediaPermissions({
        grantPermissions: async (permissions, options) => {
            calls.push({ permissions, origin: options?.origin });
        },
    });
    assert.deepEqual(granted, [...MEETING_MEDIA_ORIGINS]);
    assert.deepEqual(
        calls.map((c) => c.origin),
        [...MEETING_MEDIA_ORIGINS],
    );
    for (const c of calls) assert.deepEqual(c.permissions, ['camera', 'microphone']);
});

test('covers both Teams origins', () => {
    assert.ok(MEETING_MEDIA_ORIGINS.includes('https://teams.microsoft.com'));
    assert.ok(MEETING_MEDIA_ORIGINS.includes('https://teams.live.com'));
});

test('a failed grant for one origin does not stop the others', async () => {
    const errors: string[] = [];
    const granted = await grantMeetingMediaPermissions(
        {
            grantPermissions: async (_permissions, options) => {
                if (options?.origin === 'https://zoom.us') throw new Error('boom');
            },
        },
        (origin) => errors.push(origin),
    );
    assert.deepEqual(errors, ['https://zoom.us']);
    assert.equal(granted.length, MEETING_MEDIA_ORIGINS.length - 1);
    assert.ok(!granted.includes('https://zoom.us'));
});
