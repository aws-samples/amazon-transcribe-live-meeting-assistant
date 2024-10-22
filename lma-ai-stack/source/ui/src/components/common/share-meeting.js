import { InvokeCommand, LambdaClient, LogType } from '@aws-sdk/client-lambda';
import { Sha256 } from '@aws-crypto/sha256-js';

import { Buffer } from 'buffer';

async function calculateSha256(payload) {
  const sha256 = new Sha256();
  sha256.update(payload);
  const hashArrayBuffer = await sha256.digest();
  const hashHex = Array.from(new Uint8Array(hashArrayBuffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  return hashHex;
}

export const shareMeetings = async (
  collectionProps,
  meetingRecipients,
  settings,
  currentCredentials,
  currentSession,
) => {
  console.debug('collectionProps - KISH', collectionProps);
  console.log('Settings', settings);
  console.log('Recipients', meetingRecipients);
  console.log('session', currentSession);

  const { REACT_APP_AWS_REGION } = process.env;
  const funcName = settings.LMAShareMeetingLambda;
  const payload = {
    callIds: collectionProps.selectedItems.map(({ callId }) => callId),
    meetingRecipients,
    accessToken: currentSession.accessToken.jwtToken,
  };

  const sha256Hash = await calculateSha256(payload);

  const client = new LambdaClient({
    region: REACT_APP_AWS_REGION,
    credentials: currentCredentials,
  });

  const command = new InvokeCommand({
    FunctionName: funcName,
    LogType: LogType.Tail,
    Payload: JSON.stringify(payload),
    CustomHeaders: {
      'x-amz-content-sha256': sha256Hash,
    },
  });

  const { Payload } = await client.send(command);
  const result = Buffer.from(Payload).toString();
  // const logs = Buffer.from(LogResult, 'base64').toString();

  console.log('Lambda result:', result);

  return result;
};

export default shareMeetings;
