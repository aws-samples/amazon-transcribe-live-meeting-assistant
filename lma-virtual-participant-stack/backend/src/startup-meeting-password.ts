/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting password, read once when the Virtual Participant starts (see
 * meeting-password-source.ts for where it comes from).
 *
 * details.ts builds the meeting details when it is imported and imports this
 * module first; the top-level `await` makes ES module evaluation finish the read
 * before details.ts reads the result, as stack-settings.ts does for the
 * Configuration page's values. A read that fails is not thrown here: index.ts
 * checks `startupMeetingPassword.ok` once the status manager exists and fails
 * the launch with a status reason the UI shows.
 */
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';

import { createLogger, registerSecretValue } from './logger.js';
import { resolveMeetingPassword, type MeetingPasswordResult } from './meeting-password-source.js';

const log = createLogger('meeting-password');

/** GetItem for the row's id and meetingPassword, the only attributes the role may read. */
async function readMeetingPasswordRow(tableName: string, vpId: string) {
  // AWS_REGION is reserved for MicroVM images, so the launcher passes the
  // region as VP_AWS_REGION; status-manager.ts resolves it the same way.
  const region = process.env.AWS_REGION || process.env.VP_AWS_REGION || 'us-east-1';
  const client = new DynamoDBClient({ region });
  try {
    const response = await client.send(
      new GetItemCommand({
        TableName: tableName,
        Key: { id: { S: vpId } },
        ProjectionExpression: '#id, #pw',
        ExpressionAttributeNames: { '#id': 'id', '#pw': 'meetingPassword' },
        ConsistentRead: true,
      }),
    );
    if (!response.Item) return null;
    return { meetingPassword: response.Item.meetingPassword?.S };
  } finally {
    client.destroy();
  }
}

export const startupMeetingPassword: MeetingPasswordResult = await resolveMeetingPassword({
  env: process.env,
  readRow: readMeetingPasswordRow,
  onRetry: (attempt, error) =>
    log.warn(`Reading the meeting password from the VP record failed (attempt ${attempt}); retrying: ${error}`),
});

if (startupMeetingPassword.ok) {
  registerSecretValue(startupMeetingPassword.password);
  log.info(
    startupMeetingPassword.origin === 'none'
      ? 'No meeting password given'
      : `Meeting password read from the ${startupMeetingPassword.origin === 'record' ? 'VP record' : 'environment'}`,
  );
} else {
  log.error(`Could not read the meeting password from the VP record: ${startupMeetingPassword.error}`);
}
