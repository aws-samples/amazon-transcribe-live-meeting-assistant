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
  console.log('collectionProps - KISH', collectionProps);
  console.log('CALLS', calls);

  const getListKeys = (callId, createdAt) => {
    const SHARDS_IN_DAY = 6;
    const SHARD_DIVIDER = 24 / SHARDS_IN_DAY;

    const now = new Date(createdAt);
    const date = now.toISOString().split('T')[0];
    const hour = now.getHours();

    const hourShard = Math.floor(hour / SHARD_DIVIDER);
    const shardPad = hourShard.toString().padStart(2, '0');

    const listPK = `cls#${date}#s#${shardPad}`;
    const listSK = `ts#${createdAt}#id#${callId}`;

    console.log('Keys PK/SK LIST', listPK, listSK);

    return { listPK, listSK };
  };

  // Get PK and SK from calls
  const callsWithKeys = collectionProps.selectedItems.map(({ callId }) => {
    console.log('callId', callId);
    const call = calls.find((c) => c.CallId === callId);
    console.log('call', call);
    let listPK = call.ListPK;
    let listSK = call.ListSK;

    if (!listPK || !listSK) {
      const result = getListKeys(call.CallId, call.CreatedAt);
      listPK = result.listPK;
      listSK = result.listSK;
    }
    return {
      listPK,
      listSK,
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
