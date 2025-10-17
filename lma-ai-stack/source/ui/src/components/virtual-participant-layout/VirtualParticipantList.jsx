/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
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
  DatePicker,
  TimeInput,
  Checkbox,
  Textarea,
} from '@awsui/components-react';
import { Link as RouterLink } from 'react-router-dom';
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
      meetingTime
      scheduledFor
      isScheduled
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

const parseMeetingInvitation = /* GraphQL */ `
  query ParseMeetingInvitation($invitationText: String!) {
    parseMeetingInvitation(invitationText: $invitationText)
  }
`;

const StatusBadge = ({ status }) => {
  const getStatusProps = (vpStatus) => {
    switch (vpStatus) {
      case 'SCHEDULED':
        return { color: 'blue', children: 'Scheduled' };
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
const renderMeetingNameCell = (item) => (
  <RouterLink to={`/virtual-participant/${item.id}`} style={{ textDecoration: 'none', color: '#0972d3' }}>
    {item.meetingName}
  </RouterLink>
);

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
    meetingDate: '',
    meetingTime: '',
  });
  const [meetingTimeError, setMeetingTimeError] = useState('');
  const [consentChecked, setConsentChecked] = useState(false);
  const [notification, setNotification] = useState(null);
  const [isCreating, setIsCreating] = useState(false);
  const [creatingType, setCreatingType] = useState(null); // 'immediate' or 'scheduled'
  const [popupNotifications, setPopupNotifications] = useState([]);
  const [sortingColumn, setSortingColumn] = useState({ sortingField: 'createdAt', sortingDescending: true });

  // Meeting invitation parser state
  const [showPasteInviteModal, setShowPasteInviteModal] = useState(false);
  const [invitationText, setInvitationText] = useState('');
  const [isParsing, setIsParsing] = useState(false);
  const [parseError, setParseError] = useState('');

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

  // Validate meeting time for scheduling
  const validateMeetingTime = (time) => {
    if (!time) {
      setMeetingTimeError('');
      return;
    }

    if (time.length !== 5) {
      setMeetingTimeError('Meeting time is incomplete.');
      return;
    }

    if (!createForm.meetingDate) {
      setMeetingTimeError('Please select a meeting date first.');
      return;
    }

    // Create meeting datetime
    const meetingDateTime = new Date(`${createForm.meetingDate}T${time}`);
    const currentTime = new Date();
    const minuteDifference = (meetingDateTime.getTime() - currentTime.getTime()) / (1000 * 60);

    if (minuteDifference >= 2) {
      setMeetingTimeError('');
    } else {
      setMeetingTimeError('Meeting time must be at least two minutes from now.');
    }
  };

  // Check if form is ready for immediate execution
  const isFormValidForImmediate = createForm.meetingName && createForm.meetingId && consentChecked;

  // Check if form is ready for scheduling
  const isFormValidForScheduling =
    isFormValidForImmediate && createForm.meetingTime && createForm.meetingDate && !meetingTimeError;

  // Handle parsing meeting invitation
  const handleParseMeetingInvitation = async () => {
    if (!invitationText.trim()) {
      setParseError('Please paste a meeting invitation');
      return;
    }

    setIsParsing(true);
    setParseError('');

    try {
      const result = await API.graphql(
        graphqlOperation(parseMeetingInvitation, {
          invitationText: invitationText.trim(),
        }),
      );

      const parsedResponse = JSON.parse(result.data.parseMeetingInvitation);

      if (parsedResponse.success && parsedResponse.data) {
        const { data } = parsedResponse;

        // Auto-fill the form with parsed data
        setCreateForm((prev) => ({
          ...prev,
          meetingName: data.meetingName || prev.meetingName,
          meetingPlatform: data.meetingPlatform || prev.meetingPlatform,
          meetingId: data.meetingId || prev.meetingId,
          meetingPassword: data.meetingPassword || prev.meetingPassword,
          meetingDate: data.meetingDate || prev.meetingDate,
          meetingTime: data.meetingTime || prev.meetingTime,
        }));

        // Close paste invite modal and open create modal
        setShowPasteInviteModal(false);
        setInvitationText('');
        setShowCreateModal(true);

        setNotification({
          type: 'success',
          content: 'Meeting invitation parsed successfully! Please review and verify the details.',
        });
      } else {
        setParseError(parsedResponse.error || 'Failed to parse meeting invitation');
      }
    } catch (error) {
      console.error('Error parsing meeting invitation:', error);
      setParseError('Failed to parse meeting invitation. Please try again or enter details manually.');
    } finally {
      setIsParsing(false);
    }
  };

  const parseStepFunctionError = (executionResult) => {
    const { output } = executionResult;
    const parsedOutput = output ? JSON.parse(output) : {};
    const errorMessage = parsedOutput.errorMessage || parsedOutput.error || '';

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

  const handleCreateParticipant = async (isScheduled = false) => {
    setIsCreating(true);
    setCreatingType(isScheduled ? 'scheduled' : 'immediate');

    try {
      const userName = user?.attributes?.email || 'test-user@example.com';

      // Calculate meeting time for scheduling
      let meetingTimestamp = null;

      if (isScheduled && createForm.meetingDate && createForm.meetingTime) {
        const meetingDateTime = new Date(`${createForm.meetingDate}T${createForm.meetingTime}`);
        meetingTimestamp = Math.floor(meetingDateTime.getTime() / 1000);
      }

      // Create VP record with scheduling information
      const vpInput = {
        meetingName: createForm.meetingName,
        meetingPlatform: createForm.meetingPlatform,
        meetingId: createForm.meetingId.replace(/ /g, ''),
        meetingPassword: createForm.meetingPassword || '',
        status: isScheduled ? 'SCHEDULED' : 'INITIALIZING',
      };

      // Add scheduling fields if this is a scheduled VP
      if (isScheduled && meetingTimestamp) {
        vpInput.meetingTime = meetingTimestamp;
        vpInput.isScheduled = true;
      }

      const vpResult = await API.graphql(
        graphqlOperation(createVirtualParticipant, {
          input: vpInput,
        }),
      );

      const virtualParticipantId = vpResult.data.createVirtualParticipant.id;

      // For immediate execution, still use Step Functions (backward compatibility)
      if (!isScheduled) {
        const sfnClient = new SFNClient({
          region: awsExports.aws_project_region,
          credentials: currentCredentials,
        });

        const sfnParams = {
          stateMachineArn: settings.LMAVirtualParticipantSchedulerStateMachine,
          input: JSON.stringify({
            apiInfo: { httpMethod: 'POST' },
            data: {
              meetingPlatform: createForm.meetingPlatform,
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
          const { output } = data;
          const parsedOutput = output ? JSON.parse(output) : {};

          if (parsedOutput.success === false || parsedOutput.error) {
            const errorMessage = parseStepFunctionError(data);
            setNotification({
              type: 'error',
              content: errorMessage,
            });
            return;
          }
        }
      }

      // Reset form and close modal
      setShowCreateModal(false);
      setCreateForm({
        meetingName: '',
        meetingPlatform: 'ZOOM',
        meetingId: '',
        meetingPassword: '',
        meetingDate: '',
        meetingTime: '',
      });
      setMeetingTimeError('');
      setConsentChecked(false);

      loadParticipants();

      // Show appropriate success message
      const scheduledDateTime = new Date(`${createForm.meetingDate}T${createForm.meetingTime}`);
      const successMessage = isScheduled
        ? `Virtual participant "${
            createForm.meetingName
          }" scheduled successfully for ${scheduledDateTime.toLocaleString()}.`
        : `Virtual participant "${createForm.meetingName}" started successfully and is joining the meeting.`;

      setNotification({
        type: 'success',
        content: successMessage,
      });
    } catch (err) {
      const { message: errorMessage } = err;
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
      setCreatingType(null);
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
      id: 'scheduledFor',
      header: 'Scheduled For',
      cell: (item) => {
        if (item.isScheduled && item.scheduledFor) {
          return new Date(item.scheduledFor).toLocaleString();
        }
        if (item.meetingTime) {
          return new Date(item.meetingTime * 1000).toLocaleString();
        }
        return '-';
      },
      sortingField: 'scheduledFor',
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
    { label: 'Teams', value: 'TEAMS', disabled: false },
    { label: 'WebEx', value: 'WEBEX', disabled: false },
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
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="normal" onClick={() => setShowPasteInviteModal(true)}>
                    Paste Meeting Invite
                  </Button>
                  <Button variant="primary" onClick={() => setShowCreateModal(true)}>
                    Create Virtual Participant
                  </Button>
                </SpaceBetween>
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
                onClick={() => handleCreateParticipant(false)}
                disabled={!isFormValidForImmediate || isCreating}
                loading={isCreating && creatingType === 'immediate'}
                loadingText="Starting Virtual Participant..."
              >
                {isCreating && creatingType === 'immediate' ? 'Starting...' : 'Join Now'}
              </Button>
              <Button
                variant="primary"
                onClick={() => handleCreateParticipant(true)}
                disabled={!isFormValidForScheduling || isCreating}
                loading={isCreating && creatingType === 'scheduled'}
                loadingText="Scheduling Virtual Participant..."
              >
                {isCreating && creatingType === 'scheduled' ? 'Scheduling...' : 'Schedule for Later'}
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

            <FormField
              label="Meeting Time (Optional)"
              description="Choose a date and time that is at least two minutes from now to schedule the participant."
              stretch
            >
              <SpaceBetween direction="horizontal" size="l">
                <DatePicker
                  onChange={({ detail }) => {
                    setCreateForm((prev) => ({ ...prev, meetingDate: detail.value }));
                    if (createForm.meetingTime) {
                      validateMeetingTime(createForm.meetingTime);
                    }
                  }}
                  value={createForm.meetingDate}
                  isDateEnabled={(date) => {
                    const currentDate = new Date();
                    currentDate.setDate(currentDate.getDate() - 1);
                    return date > currentDate;
                  }}
                  placeholder="YYYY/MM/DD"
                />
                <TimeInput
                  onChange={({ detail }) => {
                    setCreateForm((prev) => ({ ...prev, meetingTime: detail.value }));
                    validateMeetingTime(detail.value);
                  }}
                  onBlur={() => validateMeetingTime(createForm.meetingTime)}
                  value={createForm.meetingTime}
                  disabled={createForm.meetingDate.length !== 10}
                  format="hh:mm"
                  placeholder="hh:mm (24-hour format)"
                  use24Hour
                />
              </SpaceBetween>
              {meetingTimeError && <Alert type="error">{meetingTimeError}</Alert>}
            </FormField>

            <Checkbox onChange={({ detail }) => setConsentChecked(detail.checked)} checked={consentChecked}>
              I will not violate legal, corporate, or ethical restrictions that apply to meeting transcription and
              recording.
            </Checkbox>
          </SpaceBetween>
        </Form>
      </Modal>

      {/* Paste Meeting Invite Modal */}
      <Modal
        visible={showPasteInviteModal}
        onDismiss={() => {
          setShowPasteInviteModal(false);
          setInvitationText('');
          setParseError('');
          // Reset the form completely when modal is closed
          setCreateForm({
            meetingName: '',
            meetingPlatform: 'ZOOM',
            meetingId: '',
            meetingPassword: '',
            meetingDate: '',
            meetingTime: '',
          });
          setMeetingTimeError('');
          setConsentChecked(false);
        }}
        header="Paste Meeting Invitation"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowPasteInviteModal(false);
                  setInvitationText('');
                  setParseError('');
                  // Reset the form completely when modal is cancelled
                  setCreateForm({
                    meetingName: '',
                    meetingPlatform: 'ZOOM',
                    meetingId: '',
                    meetingPassword: '',
                    meetingDate: '',
                    meetingTime: '',
                  });
                  setMeetingTimeError('');
                  setConsentChecked(false);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleParseMeetingInvitation}
                disabled={!invitationText.trim() || isParsing}
                loading={isParsing}
                loadingText="Parsing invitation..."
              >
                {isParsing ? 'Parsing...' : 'Parse Invitation'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween direction="vertical" size="l">
            <FormField
              label="Meeting Invitation"
              description="Paste your meeting invitation text here. The AI will extract meeting details."
              stretch
            >
              <Textarea
                value={invitationText}
                onChange={({ detail }) => {
                  setInvitationText(detail.value);
                  setParseError('');
                }}
                placeholder="Paste your meeting invitation here...

Example:
Join Zoom Meeting
https://zoom.us/j/1234567890?pwd=abcdef

Meeting ID: 123 456 7890
Passcode: 123456

Topic: Weekly Team Standup
Time: Dec 15, 2024 02:00 PM Eastern Time (US and Canada)"
                rows={12}
              />
            </FormField>

            {parseError && <Alert type="error">{parseError}</Alert>}
          </SpaceBetween>
        </Form>
      </Modal>
    </SpaceBetween>
  );
};

export default VirtualParticipantList;
