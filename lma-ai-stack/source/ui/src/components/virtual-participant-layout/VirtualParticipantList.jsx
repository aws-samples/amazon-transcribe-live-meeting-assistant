import React, { useState, useEffect } from 'react';
import { API, graphqlOperation } from 'aws-amplify';
import PropTypes from 'prop-types';
import {
  Table,
  Box,
  SpaceBetween,
  Header,
  Badge,
  Button,
  Modal,
  Form,
  FormField,
  Input,
  Select,
  Container,
  Alert,
} from '@awsui/components-react';
import { SFNClient, StartSyncExecutionCommand } from '@aws-sdk/client-sfn';
import useAppContext from '../../contexts/app';
import awsExports from '../../aws-exports';
import useSettingsContext from '../../contexts/settings';

// Simplified GraphQL operations for new schema
const listVirtualParticipants = /* GraphQL */ `
  query ListVirtualParticipants {
    listVirtualParticipants {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      createdAt
      updatedAt
    }
  }
`;

const createVirtualParticipant = /* GraphQL */ `
  mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
    createVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      createdAt
    }
  }
`;

const StatusBadge = ({ status }) => {
  const getStatusProps = (vpStatus) => {
    switch (vpStatus) {
      case 'JOINING':
        return { color: 'yellow', children: 'Joining' };
      case 'JOINED':
        return { color: 'blue', children: 'Joined' };
      case 'COMPLETED':
        return { color: 'green', children: 'Completed' };
      case 'FAILED':
        return { color: 'red', children: 'Failed' };
      default:
        return { color: 'grey', children: vpStatus };
    }
  };

  const statusProps = getStatusProps(status);
  return <Badge color={statusProps.color}>{statusProps.children}</Badge>;
};

StatusBadge.propTypes = {
  status: PropTypes.string.isRequired,
};

// Render function for status cell - defined outside component to avoid re-creation
const renderStatusCell = (item) => <StatusBadge status={item.status} />;

const VirtualParticipantList = () => {
  const { user, currentCredentials } = useAppContext();
  const { settings } = useSettingsContext();

  const [participants, setParticipants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedItems, setSelectedItems] = useState([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createForm, setCreateForm] = useState({
    meetingName: '',
    meetingPlatform: 'ZOOM',
    meetingId: '',
    meetingPassword: '',
  });
  const [notification, setNotification] = useState(null);
  const [isCreating, setIsCreating] = useState(false);

  const loadParticipants = async () => {
    try {
      setLoading(true);
      console.log('Loading virtual participants...');
      const result = await API.graphql(graphqlOperation(listVirtualParticipants));
      console.log('GraphQL result:', JSON.stringify(result, null, 2));
      console.log('VirtualParticipants array:', result.data?.listVirtualParticipants);
      setParticipants(result.data.listVirtualParticipants || []);
    } catch (error) {
      console.error('Error loading participants:', error);
      console.error('Full error:', JSON.stringify(error, null, 2));
      setNotification({
        type: 'error',
        content: 'Failed to load virtual participants',
      });
    } finally {
      setLoading(false);
    }
  };

  // Load participants on component mount
  useEffect(() => {
    loadParticipants();
  }, []);

  // Simple polling for reliable updates (no complex subscription issues)
  useEffect(() => {
    const pollInterval = setInterval(() => {
      // Only poll if we have participants and not currently loading
      if (participants.length > 0 && !loading && !isCreating) {
        console.log('Polling for VP status updates...');
        loadParticipants();
      }
    }, 3000); // Poll every 3 seconds

    return () => clearInterval(pollInterval);
  }, [participants.length, loading, isCreating]);

  const parseStepFunctionError = (executionResult) => {
    const output = executionResult.output ? JSON.parse(executionResult.output) : {};
    const errorMessage = output.errorMessage || output.error || '';

    // Check for specific error patterns
    if (
      errorMessage.toLowerCase().includes('meeting not found') ||
      errorMessage.toLowerCase().includes('invalid meeting id')
    ) {
      return 'Meeting ID not found. Please check the meeting ID and try again.';
    }

    if (
      errorMessage.toLowerCase().includes('incorrect password') ||
      errorMessage.toLowerCase().includes('authentication failed')
    ) {
      return 'Incorrect meeting password. Please check the password and try again.';
    }

    if (
      errorMessage.toLowerCase().includes('meeting not started') ||
      errorMessage.toLowerCase().includes('meeting has not begun')
    ) {
      return 'Meeting has not started yet. Please wait for the host to start the meeting.';
    }

    if (
      errorMessage.toLowerCase().includes('meeting ended') ||
      errorMessage.toLowerCase().includes('meeting has ended')
    ) {
      return 'This meeting has already ended. Please check the meeting details.';
    }

    if (
      errorMessage.toLowerCase().includes('permission denied') ||
      errorMessage.toLowerCase().includes('not authorized')
    ) {
      return 'Permission denied. You may not have access to join this meeting.';
    }

    // Generic error
    return errorMessage || 'Failed to join meeting. Please check your meeting details and try again.';
  };

  const handleCreateParticipant = async () => {
    setIsCreating(true);

    try {
      const userName = user?.attributes?.email || 'test-user@example.com';

      // Step 1: Create Virtual Participant record first (simplified)
      console.log('Creating Virtual Participant record...');
      const vpResult = await API.graphql(
        graphqlOperation(createVirtualParticipant, {
          input: {
            meetingName: createForm.meetingName,
            meetingPlatform: createForm.meetingPlatform,
            meetingId: createForm.meetingId.replace(/ /g, ''),
            meetingPassword: createForm.meetingPassword || '',
            status: 'JOINING',
          },
        }),
      );

      const virtualParticipantId = vpResult.data.createVirtualParticipant.id;
      console.log('Created VP with ID:', virtualParticipantId);

      // Step 2: Start Step Function with VP ID
      const sfnClient = new SFNClient({
        region: awsExports.aws_project_region,
        credentials: currentCredentials,
      });

      const sfnParams = {
        stateMachineArn: settings.LMAVirtualParticipantSchedulerStateMachine,
        input: JSON.stringify({
          apiInfo: { httpMethod: 'POST' },
          data: {
            meetingPlatform: createForm.meetingPlatform === 'ZOOM' ? 'Zoom' : createForm.meetingPlatform,
            meetingID: createForm.meetingId.replace(/ /g, ''),
            meetingPassword: createForm.meetingPassword,
            meetingName: createForm.meetingName,
            meetingTime: '', // for later use when supporting scheduled meetings
            userName,
            virtualParticipantId, // Now included!
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
        const errorMessage = parseStepFunctionError(data);
        setNotification({
          type: 'error',
          content: errorMessage,
        });
        return;
      }

      if (data.status === 'SUCCEEDED') {
        // Parse output to check for join success
        const output = data.output ? JSON.parse(data.output) : {};

        if (output.success === false || output.error) {
          const errorMessage = parseStepFunctionError(data);
          setNotification({
            type: 'error',
            content: errorMessage,
          });
          return;
        }
      }

      // Success - close modal and refresh list
      setShowCreateModal(false);
      setCreateForm({
        meetingName: '',
        meetingPlatform: 'ZOOM',
        meetingId: '',
        meetingPassword: '',
      });

      // Refresh the list
      loadParticipants();

      setNotification({
        type: 'success',
        content: `Virtual participant "${createForm.meetingName}" started successfully and is joining the meeting.`,
      });
    } catch (err) {
      console.error('Error starting virtual participant:', err);

      // Try to parse the error message for more specific feedback
      const errorMessage = err.message || '';
      if (errorMessage.includes('StateMachineDoesNotExist')) {
        setNotification({
          type: 'error',
          content: 'Virtual Participant service is not configured. Please contact your administrator.',
        });
      } else if (errorMessage.includes('AccessDenied')) {
        setNotification({
          type: 'error',
          content: 'You do not have permission to start a virtual participant. Please contact your administrator.',
        });
      } else if (errorMessage.includes('InvalidParameterValue')) {
        setNotification({
          type: 'error',
          content: 'Invalid meeting information provided. Please check your inputs and try again.',
        });
      } else if (errorMessage.includes('GraphQL')) {
        setNotification({
          type: 'error',
          content: 'Failed to create virtual participant record. Please try again.',
        });
      } else {
        setNotification({
          type: 'error',
          content: 'Failed to start virtual participant. Please check your meeting details and try again.',
        });
      }
    } finally {
      setIsCreating(false);
    }
  };

  const columnDefinitions = [
    {
      id: 'meetingName',
      header: 'Meeting Name',
      cell: (item) => item.meetingName,
      sortingField: 'meetingName',
    },
    {
      id: 'meetingPlatform',
      header: 'Platform',
      cell: (item) => item.meetingPlatform,
      sortingField: 'meetingPlatform',
    },
    {
      id: 'meetingId',
      header: 'Meeting ID',
      cell: (item) => item.meetingId,
      sortingField: 'meetingId',
    },
    {
      id: 'status',
      header: 'Status',
      cell: renderStatusCell,
      sortingField: 'status',
    },
    {
      id: 'createdAt',
      header: 'Created',
      cell: (item) => new Date(item.createdAt).toLocaleString(),
      sortingField: 'createdAt',
    },
  ];

  const platformOptions = [
    { label: 'Zoom', value: 'ZOOM' },
    { label: 'Chime', value: 'CHIME' },
    { label: 'Teams', value: 'TEAMS' },
    { label: 'WebEx', value: 'WEBEX' },
    { label: 'Google Meet', value: 'GOOGLE_MEET' },
  ];

  return (
    <SpaceBetween direction="vertical" size="l">
      {notification && (
        <Alert type={notification.type} dismissible onDismiss={() => setNotification(null)}>
          {notification.content}
        </Alert>
      )}

      <Container>
        <Table
          columnDefinitions={columnDefinitions}
          items={participants}
          loading={loading}
          selectedItems={selectedItems}
          onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
          selectionType="multi"
          header={
            <Header
              counter={`(${participants.length})`}
              actions={
                <Button variant="primary" onClick={() => setShowCreateModal(true)}>
                  Create Virtual Participant
                </Button>
              }
            >
              Virtual Participants
            </Header>
          }
          empty={
            <Box textAlign="center" color="inherit">
              <b>No virtual participants</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                No virtual participants found.
              </Box>
              <Button onClick={() => setShowCreateModal(true)}>Create Virtual Participant</Button>
            </Box>
          }
          sortingDisabled={false}
        />
      </Container>

      <Modal
        visible={showCreateModal}
        onDismiss={() => setShowCreateModal(false)}
        header="Create Virtual Participant"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowCreateModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleCreateParticipant}
                disabled={!createForm.meetingName || !createForm.meetingId || isCreating}
                loading={isCreating}
                loadingText="Starting Virtual Participant..."
              >
                {isCreating ? 'Starting...' : 'Join Now'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween direction="vertical" size="l">
            <FormField label="Meeting Name" stretch>
              <Input
                value={createForm.meetingName}
                onChange={({ detail }) => setCreateForm((prev) => ({ ...prev, meetingName: detail.value }))}
                placeholder="Enter meeting name"
              />
            </FormField>

            <FormField label="Meeting Platform" stretch>
              <Select
                selectedOption={platformOptions.find((opt) => opt.value === createForm.meetingPlatform)}
                onChange={({ detail }) =>
                  setCreateForm((prev) => ({ ...prev, meetingPlatform: detail.selectedOption.value }))
                }
                options={platformOptions}
              />
            </FormField>

            <FormField label="Meeting ID" stretch>
              <Input
                value={createForm.meetingId}
                onChange={({ detail }) => setCreateForm((prev) => ({ ...prev, meetingId: detail.value }))}
                placeholder="Enter meeting ID or URL"
              />
            </FormField>

            <FormField label="Meeting Password (Optional)" stretch>
              <Input
                value={createForm.meetingPassword}
                onChange={({ detail }) => setCreateForm((prev) => ({ ...prev, meetingPassword: detail.value }))}
                placeholder="Enter meeting password if required"
                type="password"
              />
            </FormField>
          </SpaceBetween>
        </Form>
      </Modal>
    </SpaceBetween>
  );
};

export default VirtualParticipantList;
