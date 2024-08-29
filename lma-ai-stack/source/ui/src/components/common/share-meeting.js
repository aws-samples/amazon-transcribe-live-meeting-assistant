import { InvokeCommand, LambdaClient, LogType } from '@aws-sdk/client-lambda';

export const shareMeetings = async (collectionProps, meetingRecipients, settings, currentCredentials) => {
  console.debug('collectionProps - KISH', collectionProps);
  console.log('Settings', settings);
  console.log('Recipients', meetingRecipients);

  const { REACT_APP_AWS_REGION } = process.env;
  const funcName = settings.LMAShareMeetingLambda;
  const payload = { collectionProps, meetingRecipients };

  // add region to client params if not present
  const params = { region: REACT_APP_AWS_REGION, credentials: currentCredentials };

  const client = new LambdaClient(params);
  const command = new InvokeCommand({
    FunctionName: funcName,
    Payload: payload,
    LogType: LogType.Tail,
  });

  const { Payload, LogResult } = await client.send(command);
  const result = Buffer.from(Payload).toString();
  const logs = Buffer.from(LogResult, 'base64').toString();

  console.log('Lambda result:', result);
  console.log('Lambda log:', logs);

  return { logs, result };
};

export default shareMeetings;
