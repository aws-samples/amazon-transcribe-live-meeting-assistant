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
import { meetingPlatformName, preparationFromVpRow, preparationStartFields } from './meeting-preparation.js';

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

test('a VP with a summary profile creates its meeting with it', () => {
  const preparation = preparationFromVpRow({ summaryProfile: 'discovery-session' });
  assert.equal(preparation?.summaryProfile, 'discovery-session');
  assert.deepEqual(preparationStartFields(preparation, 'alice@example.com'), {
    SummaryProfile: 'discovery-session',
  });
});

test('the default or a malformed summary profile is not put on the meeting', () => {
  for (const summaryProfile of ['default', '', 'Not A Name', 7]) {
    assert.equal(preparationFromVpRow({ summaryProfile }), null);
  }
  assert.equal('SummaryProfile' in preparationStartFields(preparationFromVpRow(ROW), 'alice@example.com'), false);
});

test('a VP with live guidance on or off creates its meeting with that choice', () => {
  for (const guidanceEnabled of [true, false]) {
    const preparation = preparationFromVpRow({ guidanceEnabled });
    assert.equal(preparation?.guidanceEnabled, guidanceEnabled);
    assert.deepEqual(preparationStartFields(preparation, 'alice@example.com'), {
      GuidanceEnabled: guidanceEnabled,
    });
  }
});

test("a VP without a live guidance choice leaves it to the deployment's setting", () => {
  for (const guidanceEnabled of [undefined, null, 'true', 1]) {
    assert.equal(preparationFromVpRow({ guidanceEnabled }), null);
  }
  assert.equal('GuidanceEnabled' in preparationStartFields(preparationFromVpRow(ROW), 'alice@example.com'), false);
});

test('a VP with a playbook, or none, puts it in its START event for the processor', () => {
  for (const playbook of ['sprint-planning', 'none']) {
    const preparation = preparationFromVpRow({ playbook });
    assert.equal(preparation?.playbook, playbook);
    assert.deepEqual(preparationStartFields(preparation, 'alice@example.com'), { PlaybookId: playbook });
  }
});

test("a VP without a playbook leaves it to the profile's default", () => {
  for (const playbook of [undefined, null, '', 'Not A Name', '../x', 7, 'x'.repeat(81)]) {
    assert.equal(preparationFromVpRow({ playbook }), null);
  }
  assert.equal('PlaybookId' in preparationStartFields(preparationFromVpRow(ROW), 'alice@example.com'), false);
});

test('the meeting platform is named as the meeting record names it', () => {
  assert.equal(meetingPlatformName('ZOOM'), 'Zoom');
  assert.equal(meetingPlatformName('Teams'), 'Teams');
  assert.equal(meetingPlatformName('GOOGLE_MEET'), 'Google Meet');
  assert.equal(meetingPlatformName('google meet'), 'Google Meet');
  assert.equal(meetingPlatformName('Chime'), 'Chime');
  assert.equal(meetingPlatformName('other'), null);
  assert.equal(meetingPlatformName(undefined), null);
});

test('a VP with a participant list creates its meeting with it, unshared', () => {
  const now = new Date('2026-11-25T10:00:00.000Z');
  const preparation = preparationFromVpRow({
    participants: [
      { name: 'Ana Lima', email: 'ana@example.com' },
      { name: '', email: 'ANA@example.com' },
      { email: 'bo@example.org' },
      { name: 'No address' },
    ],
  });
  assert.deepEqual(preparation?.participants, [
    { name: 'Ana Lima', email: 'ana@example.com' },
    { name: '', email: 'bo@example.org' },
  ]);
  assert.deepEqual(preparationStartFields(preparation, 'alice@example.com', now), {
    Participants: [
      { name: 'Ana Lima', email: 'ana@example.com' },
      { name: '', email: 'bo@example.org' },
    ],
    ParticipantsUpdatedBy: 'alice@example.com',
    ParticipantsUpdatedAt: '2026-11-25T10:00:00.000Z',
  });
});

test('a VP without a participant list adds none to its meeting', () => {
  assert.equal(preparationFromVpRow({ participants: [] }), null);
  assert.equal(preparationFromVpRow({ participants: 'ana@example.com' }), null);
  assert.equal('Participants' in preparationStartFields(preparationFromVpRow(ROW), 'alice@example.com'), false);
});
