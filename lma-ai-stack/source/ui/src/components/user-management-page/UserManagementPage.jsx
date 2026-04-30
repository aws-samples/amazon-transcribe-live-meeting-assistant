/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';

import { listUsers, createUser, deleteUser } from '../../graphql/queries/userManagementQueries';

const client = generateClient();
const logger = new ConsoleLogger('UserManagementPage');

const ROLE_OPTIONS = [
  { label: 'User', value: 'User' },
  { label: 'Admin', value: 'Admin' },
];

const EMAIL_REGEX = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

const formatError = (err) => {
  if (!err) return 'Unknown error';
  if (err.errors && err.errors.length) return err.errors.map((e) => e.message).join('; ');
  if (err.message) return err.message;
  return String(err);
};

const RoleBadge = ({ role }) => <Badge color={role === 'Admin' ? 'red' : 'blue'}>{role}</Badge>;
RoleBadge.propTypes = { role: PropTypes.string };
RoleBadge.defaultProps = { role: 'User' };

const COLUMN_DEFINITIONS = [
  {
    id: 'email',
    header: 'Email',
    cell: (item) => item.email || item.username,
    sortingField: 'email',
    isRowHeader: true,
  },
  {
    id: 'role',
    header: 'Role',
    cell: (item) => <RoleBadge role={item.role} />,
    sortingField: 'role',
  },
  {
    id: 'status',
    header: 'Status',
    cell: (item) => item.status || '-',
    sortingField: 'status',
  },
  {
    id: 'enabled',
    header: 'Enabled',
    cell: (item) => (item.enabled === false ? 'No' : 'Yes'),
  },
  {
    id: 'createdAt',
    header: 'Created',
    cell: (item) => (item.createdAt ? new Date(item.createdAt).toLocaleString() : '-'),
    sortingField: 'createdAt',
  },
];

const UserManagementPage = () => {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filterText, setFilterText] = useState('');
  const [selected, setSelected] = useState([]);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [newEmail, setNewEmail] = useState('');
  const [newRole, setNewRole] = useState(ROLE_OPTIONS[0]);
  const [createBusy, setCreateBusy] = useState(false);
  const [createErr, setCreateErr] = useState(null);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteErr, setDeleteErr] = useState(null);

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.graphql({ query: listUsers });
      const list = result?.data?.listUsers?.users || [];
      setUsers(list);
    } catch (err) {
      logger.error('listUsers failed', err);
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  const filteredUsers = useMemo(() => {
    if (!filterText) return users;
    const q = filterText.toLowerCase();
    return users.filter(
      (u) =>
        (u.email || '').toLowerCase().includes(q) ||
        (u.username || '').toLowerCase().includes(q) ||
        (u.role || '').toLowerCase().includes(q) ||
        (u.status || '').toLowerCase().includes(q),
    );
  }, [users, filterText]);

  const handleOpenCreate = () => {
    setNewEmail('');
    setNewRole(ROLE_OPTIONS[0]);
    setCreateErr(null);
    setCreateOpen(true);
  };

  const handleCreate = async () => {
    setCreateErr(null);
    const email = (newEmail || '').trim();
    if (!EMAIL_REGEX.test(email)) {
      setCreateErr('Please enter a valid email address.');
      return;
    }
    setCreateBusy(true);
    try {
      await client.graphql({
        query: createUser,
        variables: { input: { email, role: newRole.value } },
      });
      setSuccess(`User ${email} created. An invitation email with a temporary password has been sent.`);
      setCreateOpen(false);
      await fetchUsers();
    } catch (err) {
      logger.error('createUser failed', err);
      setCreateErr(formatError(err));
    } finally {
      setCreateBusy(false);
    }
  };

  const handleOpenDelete = () => {
    setDeleteErr(null);
    setDeleteOpen(true);
  };

  const handleDelete = async () => {
    if (selected.length === 0) return;
    setDeleteBusy(true);
    setDeleteErr(null);
    const results = await Promise.all(
      selected.map(async (user) => {
        try {
          await client.graphql({
            query: deleteUser,
            variables: { input: { username: user.username } },
          });
          return null;
        } catch (err) {
          logger.error(`deleteUser failed for ${user.username}`, err);
          return `${user.email || user.username}: ${formatError(err)}`;
        }
      }),
    );
    const failures = results.filter(Boolean);
    setDeleteBusy(false);
    if (failures.length === 0) {
      setSuccess(`Deleted ${selected.length} user${selected.length > 1 ? 's' : ''}.`);
      setSelected([]);
      setDeleteOpen(false);
      await fetchUsers();
    } else {
      setDeleteErr(failures.join('\n'));
      await fetchUsers();
    }
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="Error">
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" dismissible onDismiss={() => setSuccess(null)}>
          {success}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h1"
            description="Create and delete LMA users (Admin only). New users get a temporary password by email."
            counter={`(${users.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={fetchUsers} loading={loading}>
                  Refresh
                </Button>
                <Button disabled={selected.length === 0} onClick={handleOpenDelete}>
                  Delete
                </Button>
                <Button variant="primary" onClick={handleOpenCreate}>
                  Create user
                </Button>
              </SpaceBetween>
            }
          >
            Users
          </Header>
        }
      >
        <Table
          columnDefinitions={COLUMN_DEFINITIONS}
          items={filteredUsers}
          loading={loading}
          loadingText="Loading users"
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
          trackBy="username"
          variant="embedded"
          empty={
            <Box textAlign="center" color="inherit">
              <b>No users</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                No users found in the Cognito user pool.
              </Box>
            </Box>
          }
          filter={
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder="Find user"
              filteringAriaLabel="Filter users"
              onChange={({ detail }) => setFilterText(detail.filteringText)}
            />
          }
        />
      </Container>

      {/* Create User Modal */}
      <Modal
        visible={createOpen}
        onDismiss={() => setCreateOpen(false)}
        header="Create user"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setCreateOpen(false)} disabled={createBusy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleCreate} loading={createBusy}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {createErr && <Alert type="error">{createErr}</Alert>}
          <FormField label="Email" description="Used as the Cognito username. An invitation email will be sent.">
            <Input
              value={newEmail}
              onChange={({ detail }) => setNewEmail(detail.value)}
              placeholder="user@example.com"
              autoFocus
              type="email"
            />
          </FormField>
          <FormField label="Role">
            <Select
              selectedOption={newRole}
              options={ROLE_OPTIONS}
              onChange={({ detail }) => setNewRole(detail.selectedOption)}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Delete User Modal */}
      <Modal
        visible={deleteOpen}
        onDismiss={() => setDeleteOpen(false)}
        header={selected.length > 1 ? `Delete ${selected.length} users` : 'Delete user'}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteOpen(false)} disabled={deleteBusy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDelete} loading={deleteBusy}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {deleteErr && (
            <Alert type="error">
              <Box variant="pre">{deleteErr}</Box>
            </Alert>
          )}
          <Box variant="p">
            Are you sure you want to permanently delete the following user
            {selected.length > 1 ? 's' : ''}? This cannot be undone.
          </Box>
          <ul>
            {selected.map((u) => (
              <li key={u.username}>
                <strong>{u.email || u.username}</strong> ({u.role})
              </li>
            ))}
          </ul>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default UserManagementPage;
