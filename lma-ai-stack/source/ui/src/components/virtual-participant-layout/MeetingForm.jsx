import React, { useState, useEffect } from 'react';
import { useHistory } from 'react-router-dom';

import {
  Form,
  SpaceBetween,
  FormField,
  Input,
  Select,
  Button,
  Modal,
  Box,
  Alert,
  Container,
} from '@awsui/components-react';
import { SFNClient, StartSyncExecutionCommand } from '@aws-sdk/client-sfn';
import useAppContext from '../../contexts/app';
import awsExports from '../../aws-exports';
import useSettingsContext from '../../contexts/settings';
import { CALLS_PATH } from '../../routes/constants';

const MeetingForm = () => {
  const { user } = useAppContext();
  const { settings } = useSettingsContext();
  const { currentCredentials } = useAppContext();
  const history = useHistory();

  // Form state
  const [meetingPlatform, setMeetingPlatform] = useState('');
  const [meetingId, setMeetingId] = useState('');
  const [meetingPassword, setMeetingPassword] = useState('');
  const [meetingName, setMeetingName] = useState('');

  // UI state
  const [isLoading, setIsLoading] = useState(false);
  const [showSuccessModal, setShowSuccessModal] = useState(false);
  const [error, setError] = useState('');
  const [countdown, setCountdown] = useState(10);
  const [submittedMeetingDetails, setSubmittedMeetingDetails] = useState({});

  const meetingPlatforms = [
    { label: 'Amazon Chime', disabled: false, value: 'Chime' },
    { label: 'Zoom', disabled: false, value: 'Zoom' },
    { label: 'Microsoft Teams', disabled: true, value: 'Teams' },
    { label: 'Google Meet', disabled: true, value: 'Meet' },
  ];

  const handleNavigateToMeetings = () => {
    setShowSuccessModal(false);
    history.push(CALLS_PATH);
  };

  const handleCloseModal = () => {
    setShowSuccessModal(false);
    setCountdown(10);
  };

  const parseStepFunctionError = (executionResult) => {
    const output = executionResult.output ? JSON.parse(executionResult.output) : {};
    const errorMessage = output.errorMessage || output.error || '';

    // Check for specific error patterns
    if (
      errorMessage.toLowerCase().includes('meeting not found') ||
      errorMessage.toLowerCase().includes('invalid meeting id')
    ) {
      return {
        type: 'INVALID_MEETING_ID',
        message: 'Meeting ID not found. Please check the meeting ID and try again.',
        field: 'meetingId',
      };
    }

    if (
      errorMessage.toLowerCase().includes('incorrect password') ||
      errorMessage.toLowerCase().includes('authentication failed')
    ) {
      return {
        type: 'INVALID_PASSWORD',
        message: 'Incorrect meeting password. Please check the password and try again.',
        field: 'meetingPassword',
      };
    }

    if (
      errorMessage.toLowerCase().includes('meeting not started') ||
      errorMessage.toLowerCase().includes('meeting has not begun')
    ) {
      return {
        type: 'MEETING_NOT_STARTED',
        message: 'Meeting has not started yet. Please wait for the host to start the meeting.',
        field: 'general',
      };
    }

    if (
      errorMessage.toLowerCase().includes('meeting ended') ||
      errorMessage.toLowerCase().includes('meeting has ended')
    ) {
      return {
        type: 'MEETING_ENDED',
        message: 'This meeting has already ended. Please check the meeting details.',
        field: 'general',
      };
    }

    if (
      errorMessage.toLowerCase().includes('permission denied') ||
      errorMessage.toLowerCase().includes('not authorized')
    ) {
      return {
        type: 'PERMISSION_DENIED',
        message: 'Permission denied. You may not have access to join this meeting.',
        field: 'general',
      };
    }

    // Generic error
    return {
      type: 'GENERIC_ERROR',
      message: errorMessage || 'Failed to join meeting. Please check your meeting details and try again.',
      field: 'general',
    };
  };

  const submitMeetingForm = async () => {
    setIsLoading(true);
    setError('');

    try {
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

      const data = await sfnClient.send(new StartSyncExecutionCommand(sfnParams));
      console.log('StepFunctions response:', JSON.stringify(data));

      // Check execution status
      if (data.status === 'FAILED') {
        const errorInfo = parseStepFunctionError(data);
        setError(errorInfo.message);
        return;
      }

      if (data.status === 'SUCCEEDED') {
        // Parse output to check for join success
        const output = data.output ? JSON.parse(data.output) : {};

        if (output.success === false || output.error) {
          const errorInfo = parseStepFunctionError(data);
          setError(errorInfo.message);
          return;
        }
      }

      // Save meeting details for modal display
      setSubmittedMeetingDetails({
        name: meetingName,
        platform: meetingPlatform?.label || meetingPlatform,
        id: meetingId,
      });

      // Success - show modal and start countdown
      setShowSuccessModal(true);
      setCountdown(10);

      // Clear form
      setMeetingId('');
      setMeetingPassword('');
      setMeetingName('');
      setMeetingPlatform('');
    } catch (err) {
      console.error('Error fetching StepFunctions response:', err);

      // Try to parse the error message for more specific feedback
      const errorMessage = err.message || '';
      if (errorMessage.includes('StateMachineDoesNotExist')) {
        setError('Virtual Participant service is not configured. Please contact your administrator.');
      } else if (errorMessage.includes('AccessDenied')) {
        setError('You do not have permission to start a virtual participant. Please contact your administrator.');
      } else if (errorMessage.includes('InvalidParameterValue')) {
        setError('Invalid meeting information provided. Please check your inputs and try again.');
      } else {
        setError('Failed to start virtual participant. Please check your meeting details and try again.');
      }
    } finally {
      setIsLoading(false);
    }
  };

  // Auto-redirect countdown effect
  useEffect(() => {
    let timer;
    if (showSuccessModal && countdown > 0) {
      timer = setTimeout(() => {
        setCountdown(countdown - 1);
      }, 1000);
    } else if (showSuccessModal && countdown === 0) {
      handleNavigateToMeetings();
    }
    return () => clearTimeout(timer);
  }, [showSuccessModal, countdown, handleNavigateToMeetings]);

  const getMeetingIdDescription = () => {
    if (meetingPlatform?.value === 'Zoom') {
      return 'Enter Zoom meeting ID (e.g., 123-456-7890) or full meeting URL';
    }
    if (meetingPlatform?.value === 'Chime') {
      return 'Enter Amazon Chime meeting ID or full meeting URL';
    }
    return 'Enter the meeting ID or URL provided by the meeting host';
  };

  const getMeetingIdPlaceholder = () => {
    if (meetingPlatform?.value === 'Zoom') {
      return 'e.g., 123-456-7890 or https://zoom.us/j/1234567890';
    }
    if (meetingPlatform?.value === 'Chime') {
      return 'e.g., https://chime.aws/1234567890';
    }
    return 'Enter the meeting ID or URL';
  };

  return (
    <>
      <form
        id="meetingForm"
        onSubmit={(e) => {
          e.preventDefault();
          submitMeetingForm();
        }}
      >
        <Form variant="embedded">
          <SpaceBetween direction="vertical" size="l">
            {error && (
              <Alert type="error" dismissible onDismiss={() => setError('')}>
                {error}
              </Alert>
            )}

            <FormField label="Meeting Name">
              <Input
                onChange={({ detail }) => setMeetingName(detail.value.replace(/[/?#%+&]/g, '|'))}
                value={meetingName}
                disabled={isLoading}
                placeholder="Enter a descriptive name for your meeting"
              />
            </FormField>

            <FormField label="Meeting Platform">
              <Select
                onChange={({ detail }) => setMeetingPlatform(detail.selectedOption)}
                options={meetingPlatforms}
                selectedOption={meetingPlatform}
                disabled={isLoading}
                placeholder="Choose your meeting platform"
              />
            </FormField>

            <FormField label="Meeting ID" description={getMeetingIdDescription()}>
              <Input
                onChange={({ detail }) => setMeetingId(detail.value)}
                value={meetingId}
                disabled={isLoading}
                placeholder={getMeetingIdPlaceholder()}
              />
            </FormField>

            <FormField label="Meeting Password (if applicable)">
              <Input
                onChange={({ detail }) => setMeetingPassword(detail.value)}
                value={meetingPassword}
                type="password"
                disabled={isLoading}
                placeholder="Enter meeting password if required"
              />
            </FormField>

            <FormField>
              <SpaceBetween direction="horizontal" size="l">
                <Button
                  variant="primary"
                  form="meetingForm"
                  disabled={!meetingId || !meetingName || !meetingPlatform || isLoading}
                  loading={isLoading}
                  loadingText="Starting Virtual Participant..."
                >
                  {isLoading ? 'Starting...' : 'Join Now'}
                </Button>
              </SpaceBetween>
            </FormField>
          </SpaceBetween>
        </Form>
      </form>

      {/* Success Modal */}
      <Modal
        visible={showSuccessModal}
        onDismiss={handleCloseModal}
        size="medium"
        header="Virtual Participant Started Successfully!"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleCloseModal}>
                Stay Here
              </Button>
              <Button variant="primary" onClick={handleNavigateToMeetings}>
                Go to Meetings ({countdown}s)
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween direction="vertical" size="m">
          <Alert type="success" statusIconAriaLabel="Success">
            Your virtual participant has been successfully started and is joining the meeting.
          </Alert>

          <Container>
            <SpaceBetween direction="vertical" size="s">
              <Box variant="h4">Meeting Details:</Box>
              <Box>
                <strong>Meeting Name:</strong> {submittedMeetingDetails.name || 'N/A'}
              </Box>
              <Box>
                <strong>Platform:</strong> {submittedMeetingDetails.platform || 'N/A'}
              </Box>
              <Box>
                <strong>Meeting ID:</strong> {submittedMeetingDetails.id || 'N/A'}
              </Box>
            </SpaceBetween>
          </Container>

          <Box variant="p">
            You will be automatically redirected to the meetings page in {countdown} seconds where you can monitor the
            virtual participant&apos;s activity and view the meeting transcript.
          </Box>
        </SpaceBetween>
      </Modal>
    </>
  );
};
export default MeetingForm;
