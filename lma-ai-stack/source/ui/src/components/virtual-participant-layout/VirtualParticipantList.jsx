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
  Flashbar,
  Link,
} from '@awsui/components-react';
import { SFNClient, StartSyncExecutionCommand } from '@aws-sdk/client-sfn';
import useAppContext from '../../contexts/app';
import awsExports from '../../aws-exports';
import useSettingsContext from '../../contexts/settings';

const listVirtualParticipants = `
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
      case 'INITIALIZING':
        return { color: 'blue', children: 'Initializing' };
      case 'CONNECTING':
        return { color: 'blue', children: 'Connecting' };
      case 'JOINING':
        return { color: 'blue', children: 'Joining' };
      case 'JOINED':
        return { color: 'green', children: 'Joined' };
      case 'ACTIVE':
        return { color: 'green', children: 'Active' };
      case 'COMPLETED':
        return { color: 'green', children: 'Completed' };
      case 'FAILED':
        return { color: 'red', children: 'Failed' };
      default:
        return { color: 'grey', children: vpStatus || 'Unknown' };
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

// Render function for meeting name cell - defined outside component to avoid re-creation
const renderMeetingNameCell = (item) => <Link href={`#/virtual-participant/${item.id}`}>{item.meetingName}</Link>;

const VirtualParticipantList = () => {
  const { user, currentCredentials } = useAppContext();
  const { settings } = useSettingsContext();

  const [participants, setParticipants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createForm, setCreateForm] = useState({
    meetingName: '',
    meetingPlatform: 'ZOOM',
    meetingId: '',
    meetingPassword: '',
  });
  const [notification, setNotification] = useState(null);
  const [isCreating, setIsCreating] = useState(false);
  const [popupNotifications, setPopupNotifications] = useState([]);
  const [sortingColumn, setSortingColumn] = useState({ sortingField: 'createdAt', sortingDescending: true });

  const loadParticipants = async () => {
    try {
      setLoading(true);
      const result = await API.graphql(graphqlOperation(listVirtualParticipants));
      setParticipants(result.data.listVirtualParticipants || []);
    } catch (error) {
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

  useEffect(() => {
    const onUpdateVirtualParticipant = /* GraphQL */ `
      subscription OnUpdateVirtualParticipant {
        onUpdateVirtualParticipant {
          id
          status
          updatedAt
          meetingName
          owner
          Owner
          SharedWith
        }
      }
    `;

    const subscription = API.graphql(graphqlOperation(onUpdateVirtualParticipant)).subscribe({
      next: ({ value }) => {
        const updatedParticipant = value?.data?.onUpdateVirtualParticipant;

        if (!updatedParticipant || !updatedParticipant.id) {
          return;
        }

        setParticipants((prev) => {
          const existingParticipant = prev.find((p) => p.id === updatedParticipant.id);
          const meetingName = updatedParticipant.meetingName || existingParticipant?.meetingName || 'Unknown Meeting';
          // Only show notification if status actually changed
          if (
            existingParticipant &&
            existingParticipant.status !== updatedParticipant.status &&
            updatedParticipant.status
          ) {
            let notificationType = 'info';
            let message = '';

            switch (updatedParticipant.status) {
              case 'INITIALIZING':
                notificationType = 'info';
                message = `Virtual Participant initializing for "${meetingName}"`;
                break;
              case 'CONNECTING':
                notificationType = 'info';
                message = `Virtual Participant connecting to "${meetingName}"`;
                break;
              case 'JOINING':
                notificationType = 'info';
                message = `Virtual Participant joining "${meetingName}"`;
                break;
              case 'JOINED':
                notificationType = 'success';
                message = `Virtual Participant joined "${meetingName}"`;
                break;
              case 'ACTIVE':
                notificationType = 'success';
                message = `Virtual Participant is active in "${meetingName}"`;
                break;
              case 'COMPLETED':
                notificationType = 'success';
                message = `Virtual Participant completed "${meetingName}"`;
                break;
              case 'FAILED':
                notificationType = 'error';
                message = `Virtual Participant failed to join "${meetingName}"`;
                break;
              default:
                message = `Virtual Participant status updated to ${updatedParticipant.status} for "${meetingName}"`;
            }

            // Check if a similar notification already exists to prevent duplicates
            setPopupNotifications((current) => {
              const existingNotification = current.find((n) => n.content === message && n.type === notificationType);

              // If similar notification already exists, don't add a new one
              if (existingNotification) {
                return current;
              }

              const notificationId = `vp-${updatedParticipant.id}-${updatedParticipant.status}-${Date.now()}`;
              const popupNotification = {
                type: notificationType,
                content: message,
                dismissible: true,
                dismissLabel: 'Dismiss',
                id: notificationId,
                onDismiss: () => {
                  setPopupNotifications((notifications) => notifications.filter((n) => n.id !== notificationId));
                },
              };

              // Auto-dismiss after 8 seconds
              setTimeout(() => {
                setPopupNotifications((notifications) => notifications.filter((n) => n.id !== notificationId));
              }, 8000);

              return [...current, popupNotification];
            });
          }

          return prev.map((p) => {
            if (p.id === updatedParticipant.id) {
              return {
                ...p,
                status: updatedParticipant.status,
                updatedAt: updatedParticipant.updatedAt,
              };
            }
            return p;
          });
        });
      },
      error: () => {
        const pollInterval = setInterval(() => {
          loadParticipants();
        }, 5000);

        return () => clearInterval(pollInterval);
      },
    });

    return () => subscription.unsubscribe();
  }, []); // Remove dependencies to prevent subscription recreation

  // Sorting function
  const sortParticipants = (items, sortingConfig) => {
    if (!sortingConfig.sortingField) return items;

    return [...items].sort((a, b) => {
      const aValue = a[sortingConfig.sortingField];
      const bValue = b[sortingConfig.sortingField];

      let comparison = 0;

      // Handle different data types
      if (sortingConfig.sortingField === 'createdAt') {
        comparison = new Date(aValue) - new Date(bValue);
      } else if (typeof aValue === 'string' && typeof bValue === 'string') {
        comparison = aValue.toLowerCase().localeCompare(bValue.toLowerCase());
      } else if (aValue < bValue) {
        comparison = -1;
      } else if (aValue > bValue) {
        comparison = 1;
      } else {
        comparison = 0;
      }

      return sortingConfig.sortingDescending ? -comparison : comparison;
    });
  };

  // Handle sorting change
  const handleSortingChange = ({ detail }) => {
    const newSortingField = detail.sortingColumn?.sortingField || detail.sortingField;

    // If clicking the same column, toggle the direction
    if (sortingColumn.sortingField === newSortingField) {
      setSortingColumn({
        sortingField: newSortingField,
        sortingDescending: !sortingColumn.sortingDescending,
      });
    } else {
      // If clicking a different column, start with ascending (false)
      setSortingColumn({
        sortingField: newSortingField,
        sortingDescending: false,
      });
    }
  };

  // Get sorted participants
  const sortedParticipants = sortParticipants(participants, sortingColumn);

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

      const vpResult = await API.graphql(
        graphqlOperation(createVirtualParticipant, {
          input: {
            meetingName: createForm.meetingName,
            meetingPlatform: createForm.meetingPlatform,
            meetingId: createForm.meetingId.replace(/ /g, ''),
            meetingPassword: createForm.meetingPassword || '',
            status: 'INITIALIZING',
          },
        }),
      );

      const virtualParticipantId = vpResult.data.createVirtualParticipant.id;

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
            meetingTime: '',
            userName,
            virtualParticipantId,
            accessToken: user.signInUserSession.accessToken.jwtToken,
            idToken: user.signInUserSession.idToken.jwtToken,
            rereshToken: user.signInUserSession.refreshToken.token,
          },
        }),
      };

      const data = await sfnClient.send(new StartSyncExecutionCommand(sfnParams));

      if (data.status === 'FAILED') {
        const errorMessage = parseStepFunctionError(data);
        setNotification({
          type: 'error',
          content: errorMessage,
        });
        return;
      }

      if (data.status === 'SUCCEEDED') {
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

      setShowCreateModal(false);
      setCreateForm({
        meetingName: '',
        meetingPlatform: 'ZOOM',
        meetingId: '',
        meetingPassword: '',
      });

      loadParticipants();

      setNotification({
        type: 'success',
        content: `Virtual participant "${createForm.meetingName}" started successfully and is joining the meeting.`,
      });
    } catch (err) {
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
      cell: renderMeetingNameCell,
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
    { label: 'Zoom', value: 'ZOOM', disabled: false },
    { label: 'Chime', value: 'CHIME', disabled: false },
    { label: 'Teams', value: 'TEAMS', disabled: true },
    { label: 'WebEx', value: 'WEBEX', disabled: true },
    { label: 'Google Meet', value: 'GOOGLE_MEET', disabled: true },
  ];

  return (
    <SpaceBetween direction="vertical" size="l">
      {/* Popup notifications using Flashbar */}
      {popupNotifications.length > 0 && <Flashbar items={popupNotifications} />}

      {notification && (
        <Alert type={notification.type} dismissible onDismiss={() => setNotification(null)}>
          {notification.content}
        </Alert>
      )}

      <Container>
        <Table
          columnDefinitions={columnDefinitions}
          items={sortedParticipants}
          loading={loading}
          sortingColumn={sortingColumn}
          onSortingChange={handleSortingChange}
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
