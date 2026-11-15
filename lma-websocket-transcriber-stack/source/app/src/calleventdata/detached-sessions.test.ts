/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { DetachableSession, DetachedSessions } from './detached-sessions';

const session = (callId = 'meeting-1', ownerSub: string | undefined = 'user-a'): DetachableSession => ({
    callMetadata: { callId },
    ownerSub,
    ended: false,
});

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test('the owner reconnecting within the window gets the same session back', () => {
    const expired: DetachableSession[] = [];
    const held = new DetachedSessions<DetachableSession>(60_000, (s) => expired.push(s));
    const original = session();

    assert.equal(held.detach(original, 1_000), true);
    const resumed = held.resume('meeting-1', 'user-a', 4_500);

    assert.equal(resumed?.session, original);
    assert.equal(resumed?.detachedForMs, 3_500);
    assert.equal(held.size, 0);
    assert.deepEqual(expired, []);
});

test('a session is resumed only by the user who opened it', () => {
    const held = new DetachedSessions<DetachableSession>(60_000, () => undefined);
    const original = session();
    held.detach(original);

    assert.equal(held.resume('meeting-1', 'user-b'), undefined);
    assert.equal(held.resume('meeting-1', undefined), undefined);
    // Still held for its owner.
    assert.equal(held.resume('meeting-1', 'user-a')?.session, original);
    held.drain();
});

test('a session nobody resumes is ended when the window closes', async () => {
    const expired: DetachableSession[] = [];
    const held = new DetachedSessions<DetachableSession>(20, (s) => expired.push(s));
    const original = session();
    held.detach(original);

    await sleep(60);

    assert.deepEqual(expired, [original]);
    assert.equal(held.resume('meeting-1', 'user-a'), undefined);
});

test('with the window at zero, or without a verified owner, nothing is held', () => {
    assert.equal(new DetachedSessions<DetachableSession>(0, () => undefined).detach(session()), false);
    const held = new DetachedSessions<DetachableSession>(60_000, () => undefined);
    assert.equal(held.detach({ callMetadata: { callId: 'meeting-1' }, ended: false }), false);
    const ended = session();
    ended.ended = true;
    assert.equal(held.detach(ended), false);
    assert.equal(held.size, 0);
});

test('a session ended some other way is no longer held and never expires', async () => {
    const expired: DetachableSession[] = [];
    const held = new DetachedSessions<DetachableSession>(20, (s) => expired.push(s));
    const original = session();
    held.detach(original);
    held.forget(original);

    await sleep(60);

    assert.deepEqual(expired, []);
    assert.equal(held.find('meeting-1'), undefined);
});

test('shutdown takes every held session at once', async () => {
    const expired: DetachableSession[] = [];
    const held = new DetachedSessions<DetachableSession>(20, (s) => expired.push(s));
    const a = session('meeting-1');
    const b = session('meeting-2');
    held.detach(a);
    held.detach(b);

    assert.deepEqual(held.drain(), [a, b]);
    await sleep(60);
    assert.deepEqual(expired, [], 'drained sessions are ended by the caller, not by the timer');
});
