import React, { useState } from 'react';

import { Form, SpaceBetween, FormField, Input, Select, Button } from '@awsui/components-react';
import { SFNClient, StartSyncExecutionCommand } from '@aws-sdk/client-sfn';
import useAppContext from '../../contexts/app';
import awsExports from '../../aws-exports';
import useSettingsContext from '../../contexts/settings';

const MeetingForm = () => {
  const { user } = useAppContext();
  const { settings } = useSettingsContext();
  const [meetingPlatform, setMeetingPlatform] = useState('');
  const [meetingId, setMeetingId] = useState('');
  const [meetingPassword, setMeetingPassword] = useState('');
  const [meetingName, setMeetingName] = useState('');
  const { currentCredentials } = useAppContext();

  const meetingPlatforms = [
    { label: 'Amazon Chime', disabled: false, value: 'Chime' },
    { label: 'Zoom', disabled: false, value: 'Zoom' },
    { label: 'Microsoft Teams', disabled: true, value: 'Teams' },
    { label: 'Google Meet', disabled: true, value: 'Meet' },
  ];

  const submitMeetingForm = () => {
    // for later use when supporting scheduled meetings
    const meetingDateTimeFormatted = '';

    console.log('User:', JSON.stringify(user));

    const userName = user?.attributes?.email || 'Unknown';

    // get stepfunctions client
    const sfnClient = new SFNClient({
      region: awsExports.aws_project_region,
      credentials: currentCredentials,
    });

    // execute stepfunctions
    const sfnParams = {
      stateMachineArn: settings.LMAVirtualParticipantSchedulerStateMachine,
      input: JSON.stringify({
        apiInfo: { httpMethod: 'POST' },
        data: {
          meetingPlatform: meetingPlatform.value,
          meetingID: meetingId.replace(/ /g, ''),
          meetingPassword,
          meetingName,
          meetingTime: meetingDateTimeFormatted,
          userName,
          accessToken: user.signInUserSession.accessToken.jwtToken,
          idToken: user.signInUserSession.idToken.jwtToken,
          rereshToken: user.signInUserSession.refreshToken.token,
        },
      }),
    };

    console.log('StepFunctions params:', JSON.stringify(sfnParams));
    sfnClient
      .send(new StartSyncExecutionCommand(sfnParams))
      .then((data) => {
        console.log('StepFunctions response:', JSON.stringify(data));
      })
      .catch((error) => {
        console.error('Error fetching StepFunctions response:', error);
      });

    setMeetingId('');
    setMeetingPassword('');
    setMeetingName('');
  };

  return (
    <form
      id="meetingForm"
      onSubmit={(e) => {
        e.preventDefault();
        submitMeetingForm();
      }}
    >
      <Form variant="embedded">
        <SpaceBetween direction="vertical" size="l">
          <FormField label="Meeting Name">
            <Input onChange={({ detail }) => setMeetingName(detail.value)} value={meetingName} />
          </FormField>
          <FormField label="Meeting Platform">
            <Select
              onChange={({ detail }) => setMeetingPlatform(detail.selectedOption)}
              options={meetingPlatforms}
              selectedOption={meetingPlatform}
            />
          </FormField>

          <FormField label="Meeting ID">
            <Input onChange={({ detail }) => setMeetingId(detail.value)} value={meetingId} />
          </FormField>

          <FormField label="Meeting Password (if applicable)">
            <Input
              onChange={({ detail }) => setMeetingPassword(detail.value)}
              value={meetingPassword}
              type="password"
            />
          </FormField>

          <FormField>
            <SpaceBetween direction="horizontal" size="l">
              <Button variant="normal" form="meetingForm" disabled={!meetingId || !meetingName}>
                Join Now
              </Button>
            </SpaceBetween>
          </FormField>
        </SpaceBetween>
      </Form>
    </form>
  );
};
export default MeetingForm;
