/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How the VP reads its CallId and capture session at startup. A row without a
 * CallId is a first launch, which makes its own CallId. A row that cannot be
 * read is not one: it is read again, and then the launch fails, because a
 * rejoined VP that went on would start its meeting under a new CallId.
 */
import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { CallSessionReadError, readCallSession, type VpCallSession } from './call-session.js';
import { VirtualParticipantStatusManager } from './status-manager.js';

const noWait = async () => {};
const rejoined: VpCallSession = { callId: 'call-1', sessionNumber: 2, sessionTimeOffset: 1802 };

/** A status manager whose GraphQL requests are answered by `answer`. */
const managerAnswering = (answer: () => Promise<any>) => {
    process.env.GRAPHQL_ENDPOINT = 'https://example.invalid/graphql';
    const manager: any = new VirtualParticipantStatusManager('vp-1');
    manager.graphqlEndpoint = 'https://example.invalid/graphql';
    manager.signAndSendGraphQLRequest = answer;
    return manager as VirtualParticipantStatusManager;
};

describe('reading the VP row at startup', () => {
    it('answers a rejoined session from the row', async () => {
        const manager = managerAnswering(async () => ({
            getVirtualParticipant: { id: 'vp-1', CallId: 'call-1', sessionNumber: 2, sessionTimeOffset: 1802 },
        }));
        assert.deepEqual(await manager.getCallSession(), rejoined);
    });

    it('answers null for a row without a CallId, which is a first launch', async () => {
        const manager = managerAnswering(async () => ({ getVirtualParticipant: { id: 'vp-1', CallId: null } }));
        assert.equal(await manager.getCallSession(), null);
    });

    it('tells a failed read apart from a row without a CallId', async () => {
        const failing = managerAnswering(async () => {
            throw new Error('GraphQL errors: throttled');
        });
        await assert.rejects(failing.getCallSession(), CallSessionReadError);

        // An HTTP failure answers null from the request helper, and so does a
        // row that is not there; neither is a row without a CallId.
        const unanswered = managerAnswering(async () => null);
        await assert.rejects(unanswered.getCallSession(), CallSessionReadError);
    });
});

describe('readCallSession', () => {
    it('reads again after a failed read', async () => {
        let reads = 0;
        const retried: number[] = [];
        const session = await readCallSession(
            async () => {
                reads += 1;
                if (reads < 3) throw new CallSessionReadError('not now');
                return rejoined;
            },
            { sleep: noWait, onRetry: (attempt) => retried.push(attempt) },
        );
        assert.deepEqual(session, rejoined);
        assert.equal(reads, 3);
        assert.deepEqual(retried, [1, 2]);
    });

    it('fails once every read has failed, rather than answering as for a first launch', async () => {
        let reads = 0;
        await assert.rejects(
            readCallSession(
                async () => {
                    reads += 1;
                    throw new CallSessionReadError('unreadable');
                },
                { attempts: 3, sleep: noWait },
            ),
            CallSessionReadError,
        );
        assert.equal(reads, 3);
    });

    it('answers a first launch at once, without reading again', async () => {
        let reads = 0;
        const session = await readCallSession(
            async () => {
                reads += 1;
                return null;
            },
            { sleep: noWait },
        );
        assert.equal(session, null);
        assert.equal(reads, 1);
    });

    it('waits longer before each later read', async () => {
        const waits: number[] = [];
        await assert.rejects(
            readCallSession(
                async () => {
                    throw new CallSessionReadError('unreadable');
                },
                { attempts: 4, delayMs: 100, sleep: async (ms) => void waits.push(ms) },
            ),
        );
        assert.deepEqual(waits, [100, 200, 400]);
    });
});
