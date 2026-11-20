/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting preparation a VP reads from its own row and puts on the meeting's
 * START event.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { preparationFromVpRow, preparationStartFields } from './meeting-preparation.js';

const ROW = {
  labels: [{ Key: 'project', Value: 'website-relaunch' }],
  audience: 'external',
  meetingContext: '# Goals\nAgree the budget',
  checklist: [
    { ItemId: 'q1', Text: 'Who owns the budget?', Kind: 'question', Priority: 'must', Source: 'ref-1' },
    { ItemId: 't1', Text: 'Hiring plan', Kind: 'topic' },
  ],
};

test('a row without preparation yields none, so the meeting is created as before', () => {
  assert.equal(preparationFromVpRow(null), null);
  assert.equal(preparationFromVpRow(undefined), null);
  assert.equal(preparationFromVpRow({}), null);
  assert.equal(preparationFromVpRow({ labels: [], meetingContext: '  ', checklist: [], audience: 'other' }), null);
  assert.deepEqual(preparationStartFields(null, 'alice@example.com'), {});
});

test('the START fields carry all four inputs, named as CreateCallInput names them', () => {
  const now = new Date('2026-11-19T10:00:00.000Z');
  const fields = preparationStartFields(preparationFromVpRow(ROW), 'alice@example.com', now);
  assert.deepEqual(fields, {
    Labels: [{ Key: 'project', Value: 'website-relaunch' }],
    Audience: 'external',
    MeetingMetadataUpdatedAt: '2026-11-19T10:00:00.000Z',
    MeetingContext: '# Goals\nAgree the budget',
    MeetingContextUpdatedBy: 'alice@example.com',
    MeetingContextUpdatedAt: '2026-11-19T10:00:00.000Z',
    Checklist: [
      {
        ItemId: 'q1',
        Text: 'Who owns the budget?',
        Kind: 'question',
        Priority: 'must',
        Source: 'ref-1',
        Status: 'open',
        Evidence: [],
        UpdatedBy: 'alice@example.com',
        UpdatedAt: '2026-11-19T10:00:00.000Z',
      },
      {
        ItemId: 't1',
        Text: 'Hiring plan',
        Kind: 'topic',
        Priority: 'should',
        Status: 'open',
        Evidence: [],
        UpdatedBy: 'alice@example.com',
        UpdatedAt: '2026-11-19T10:00:00.000Z',
      },
    ],
    ChecklistUpdatedAt: '2026-11-19T10:00:00.000Z',
  });
});

test('only the inputs that were given are sent', () => {
  const fields = preparationStartFields(preparationFromVpRow({ audience: 'internal' }), '');
  assert.deepEqual(Object.keys(fields).sort(), ['Audience', 'MeetingMetadataUpdatedAt']);
});

test('a malformed row entry is left out rather than failing the meeting', () => {
  const preparation = preparationFromVpRow({
    labels: [{ Key: 'ok', Value: 'v' }, { Key: 5 }, null],
    checklist: [{ ItemId: 'a', Text: 'x' }, { ItemId: 'b', Text: 'y', Kind: 'risk' }],
  });
  assert.deepEqual(preparation?.labels, [{ Key: 'ok', Value: 'v' }]);
  assert.deepEqual(preparation?.checklist.map((item) => item.ItemId), ['b']);
});

test('a VP scheduled with meetingSharedWith creates its meeting shared with those people', () => {
  const preparation = preparationFromVpRow({ meetingSharedWith: 'bo@example.com, cy@example.com,bo@example.com' });
  assert.deepEqual(preparation?.meetingSharedWith, ['bo@example.com', 'cy@example.com']);
  assert.deepEqual(preparationStartFields(preparation, 'alice@example.com'), {
    SharedWith: 'bo@example.com,cy@example.com',
  });
});

test('a VP without meetingSharedWith creates its meeting unshared', () => {
  assert.equal('SharedWith' in preparationStartFields(preparationFromVpRow(ROW), 'alice@example.com'), false);
  assert.equal(preparationFromVpRow({ meetingSharedWith: ' , ' }), null);
  assert.equal(preparationFromVpRow({ meetingSharedWith: ['a@example.com'] }), null);
});
