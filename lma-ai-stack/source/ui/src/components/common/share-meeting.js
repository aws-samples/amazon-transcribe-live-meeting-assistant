import { API } from 'aws-amplify';
import meetingControls from '../../graphql/queries/meetingControls';

export const shareMeetings = async (
  calls,
  collectionProps,
  meetingRecipients,
  settings,
  currentCredentials,
  currentSession,
) => {
  const getListKeys = (callId, createdAt) => {
    const SHARDS_IN_DAY = 6;
    const SHARD_DIVIDER = 24 / SHARDS_IN_DAY;

    const now = new Date(createdAt);
    const date = now.toISOString().substring(0, 10);
    const hour = now.getUTCHours();

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

  const payload = {
    calls: callsWithKeys,
    meetingRecipients,
    accessToken: currentSession.accessToken.jwtToken,
  };

  const response = await API.graphql({
    query: meetingControls,
    variables: payload,
  });

  const result = JSON.parse(response.data.meetingControls);
  console.log('Lambda result:', result);

  return result;
};

export default shareMeetings;
