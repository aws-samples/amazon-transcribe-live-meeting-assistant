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

// GraphQL operations
const listVirtualParticipants = /* GraphQL */ `
  query ListVirtualParticipants {
    listVirtualParticipants {
      VirtualParticipants {
        VirtualParticipantId
        meetingName
        meetingPlatform
        meetingId
        status
        CreatedAt
        UpdatedAt
      }
    }
  }
`;

const createVirtualParticipant = /* GraphQL */ `
  mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
    createVirtualParticipant(input: $input) {
      VirtualParticipantId
      meetingName
      meetingPlatform
      meetingId
      status
      CreatedAt
    }
  }
`;

// Subscription temporarily disabled due to authorization issues
// const onUpdateVirtualParticipant = /* GraphQL */ `
//   subscription OnUpdateVirtualParticipant {
//     onUpdateVirtualParticipant {
//       VirtualParticipantId
//       meetingName
//       status
//       UpdatedAt
//     }
//   }
// `;

const StatusBadge = ({ status }) => {
  const getStatusProps = (vpStatus) => {
    switch (vpStatus) {
      case 'JOINING':
        return { color: 'blue', children: 'Joining' };
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

  const loadParticipants = async () => {
    try {
      setLoading(true);
      console.log('Loading virtual participants...');
      const result = await API.graphql(graphqlOperation(listVirtualParticipants));
      console.log('GraphQL result:', JSON.stringify(result, null, 2));
      console.log('VirtualParticipants array:', result.data?.listVirtualParticipants?.VirtualParticipants);
      setParticipants(result.data.listVirtualParticipants.VirtualParticipants || []);
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

  // Subscribe to real-time updates - TEMPORARILY DISABLED due to authorization issues
  // TODO: Re-enable after fixing subscription authorization
  useEffect(() => {
    console.log('Real-time subscriptions temporarily disabled - using polling instead');

    // Poll for updates every 10 seconds as fallback
    const pollInterval = setInterval(() => {
      loadParticipants();
    }, 10000);

    return () => clearInterval(pollInterval);
  }, []);

  const handleCreateParticipant = async () => {
    try {
      const userName = 'test-user@example.com'; // TODO: Get from auth context

      await API.graphql(
        graphqlOperation(createVirtualParticipant, {
          input: {
            VirtualParticipantId: `vp-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
            meetingName: createForm.meetingName,
            meetingPlatform: createForm.meetingPlatform,
            meetingId: createForm.meetingId,
            meetingPassword: createForm.meetingPassword || '',
            status: 'JOINING',
            Owner: userName,
            SharedWith: '',
            ExpiresAfter: Math.floor(Date.now() / 1000) + 30 * 24 * 60 * 60, // 30 days from now
          },
        }),
      );

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
        content: 'Virtual participant created successfully',
      });
    } catch (error) {
      console.error('Error creating participant:', error);
      setNotification({
        type: 'error',
        content: 'Failed to create virtual participant',
      });
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
      id: 'CreatedAt',
      header: 'Created',
      cell: (item) => new Date(item.CreatedAt).toLocaleString(),
      sortingField: 'CreatedAt',
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
                disabled={!createForm.meetingName || !createForm.meetingId}
              >
                Create
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
