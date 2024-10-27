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
  calls,
  collectionProps,
  meetingRecipients,
  settings,
  currentCredentials,
  currentSession,
) => {
  // Get PK and SK from calls
  const callsWithKeys = collectionProps.selectedItems.map(({ callId }) => {
    console.log('callId', callId);
    const call = calls.find((c) => c.CallId === callId);
    console.log('call', call);
    return {
      PK: call.ShardPK,
      SK: call.ShardSK,
      callId: call.CallId,
    };
  });

  console.log('Calls with PK and SK:', callsWithKeys);

  const { REACT_APP_AWS_REGION } = process.env;
  const funcName = settings.LMAShareMeetingLambda;
  const payload = {
    calls: callsWithKeys,
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
