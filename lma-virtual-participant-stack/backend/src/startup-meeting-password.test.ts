/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The startup read itself: one GetItem on the VP's row for its id and
 * meetingPassword only, and the value read registered for log redaction.
 * The module reads when it is imported, so the environment and the client are
 * set up first and it is imported dynamically.
 */
import { strict as assert } from 'node:assert';
import { mock, test } from 'node:test';
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';

import { REDACTED, captureLogs, createLogger } from './logger.js';

test('the VP reads only its id and meeting password from its row, and redacts the value', async () => {
  process.env.VIRTUAL_PARTICIPANT_ID = 'vp-startup-1';
  process.env.VP_TABLE_NAME = 'stack-VirtualParticipant';
  delete process.env.MEETING_PASSWORD;
  delete process.env.LOCAL_TEST;

  const sent: GetItemCommand['input'][] = [];
  mock.method(DynamoDBClient.prototype, 'send', async (command: GetItemCommand) => {
    sent.push(command.input);
    return { Item: { id: { S: 'vp-startup-1' }, meetingPassword: { S: 'Row-Secret-42' } } };
  });

  const { startupMeetingPassword } = await import('./startup-meeting-password.js');

  assert.deepEqual(startupMeetingPassword, { ok: true, password: 'Row-Secret-42', origin: 'record' });
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0], {
    TableName: 'stack-VirtualParticipant',
    Key: { id: { S: 'vp-startup-1' } },
    ProjectionExpression: '#id, #pw',
    ExpressionAttributeNames: { '#id': 'id', '#pw': 'meetingPassword' },
    ConsistentRead: true,
  });

  const captured = captureLogs();
  try {
    createLogger('unit').info('typed Row-Secret-42 into the passcode box');
  } finally {
    captured.restore();
  }
  assert.ok(!captured.messages()[0].includes('Row-Secret-42'));
  assert.ok(captured.messages()[0].includes(REDACTED));
});
