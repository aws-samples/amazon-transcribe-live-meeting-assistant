/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { strict as assert } from 'node:assert';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import { preparationFromVpRow, type MeetingParticipant } from './meeting-preparation.js';

type Record_ = Record<string, any>;
const sent: Record_[] = [];
const manager = kinesisStreamManager as unknown as {
    sendRecord: (record: Record_) => Promise<void>;
    sendStartMeeting: () => Promise<void>;
    startSent: boolean;
};

const ANA = { name: 'Ana Lima', email: 'ana@example.com' };
const BO = { name: '', email: 'bo@example.org' };

const start = () => sent.find((record) => record.EventType === 'START') as Record_;

beforeEach(() => {
    sent.length = 0;
    manager.sendRecord = async (record: Record_) => {
        sent.push(record);
    };
    manager.startSent = false;
    kinesisStreamManager.setPreparation(preparationFromVpRow({ labels: [{ Key: 'team', Value: 'sales' }], participants: [ANA] }));
    kinesisStreamManager.setParticipantReader(null);
});

describe('the participant list START carries', () => {
    it('is the one read again just before START, so a change made while joining reaches the meeting', async () => {
        kinesisStreamManager.setParticipantReader(async () => [ANA, BO]);
        await manager.sendStartMeeting();
        assert.deepEqual(start().Participants, [ANA, BO]);
        // The rest of the preparation is the one read at startup.
        assert.deepEqual(start().Labels, [{ Key: 'team', Value: 'sales' }]);
    });

    it('can be emptied while joining', async () => {
        kinesisStreamManager.setParticipantReader(async () => []);
        await manager.sendStartMeeting();
        assert.equal(start().Participants, undefined);
    });

    it('is added for a VP that started with no preparation at all', async () => {
        kinesisStreamManager.setPreparation(null);
        kinesisStreamManager.setParticipantReader(async () => [BO]);
        await manager.sendStartMeeting();
        assert.deepEqual(start().Participants, [BO]);
        assert.equal(start().Labels, undefined);
    });

    it('is the one read at startup when the row cannot be read again', async () => {
        kinesisStreamManager.setParticipantReader(async () => null);
        await manager.sendStartMeeting();
        assert.deepEqual(start().Participants, [ANA]);
    });

    it('is the one read at startup when the read fails', async () => {
        kinesisStreamManager.setParticipantReader(async (): Promise<MeetingParticipant[] | null> => {
            throw new Error('network');
        });
        await manager.sendStartMeeting();
        assert.deepEqual(start().Participants, [ANA]);
    });

    it('is the one read at startup without a reader (local runs)', async () => {
        await manager.sendStartMeeting();
        assert.deepEqual(start().Participants, [ANA]);
    });
});

describe('the participant list after START', () => {
    const Manager = kinesisStreamManager.constructor as unknown as {
        PARTICIPANT_SYNC_WAIT_MS: number;
        PARTICIPANT_SYNC_POLL_MS: number;
    };

    beforeEach(() => {
        Manager.PARTICIPANT_SYNC_WAIT_MS = 50;
        Manager.PARTICIPANT_SYNC_POLL_MS = 5;
        kinesisStreamManager.setParticipantSync(null);
    });

    const syncWith = (appearsAfter: number) => {
        let checks = 0;
        const written: MeetingParticipant[][] = [];
        kinesisStreamManager.setParticipantSync({
            meetingExists: async () => {
                checks += 1;
                return checks > appearsAfter;
            },
            writeMeetingList: async (_callId, participants) => {
                written.push(participants);
                return true;
            },
        });
        return written;
    };

    it('puts a change saved on the VP while the meeting was being created on the meeting, once it exists', async () => {
        const written = syncWith(2);
        kinesisStreamManager.setParticipantReader(async () => [ANA, BO]);
        await kinesisStreamManager.syncParticipantsAfterStart([ANA]);
        assert.deepEqual(written, [[ANA, BO]]);
    });

    it('writes nothing when the list did not change', async () => {
        const written = syncWith(0);
        kinesisStreamManager.setParticipantReader(async () => [ANA]);
        await kinesisStreamManager.syncParticipantsAfterStart([ANA]);
        assert.deepEqual(written, []);
    });

    it('writes nothing when the meeting record does not appear, or the list cannot be read', async () => {
        const written = syncWith(1000);
        kinesisStreamManager.setParticipantReader(async () => [BO]);
        await kinesisStreamManager.syncParticipantsAfterStart([ANA]);
        syncWith(0);
        kinesisStreamManager.setParticipantReader(async () => null);
        await kinesisStreamManager.syncParticipantsAfterStart([ANA]);
        assert.deepEqual(written, []);
    });
});
