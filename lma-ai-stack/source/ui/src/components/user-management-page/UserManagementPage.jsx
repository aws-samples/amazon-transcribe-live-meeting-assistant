/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import {
  Alert,
  Autosuggest,
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

import {
  listUsers,
  createUser,
  deleteUser,
  setUserRole,
  setUserAccountNames,
  listInvitableMeetings,
} from '../../graphql/queries/userManagementQueries';
import {
  displayNameProblem,
  namesInput,
  namesProblem,
  namesSummary,
  parseShortNames,
  shortNamesProblem,
  shortNamesText,
} from '../common/account-names';
import useAppContext from '../../contexts/app';
import { INVITABLE_MEETING_DAYS, invitableMeetingOptions, meetingRefFromText } from './invite-meeting';

const client = generateClient();
const logger = new ConsoleLogger('UserManagementPage');

// The three roles (docs/user-based-access-control.md#roles).
export const ROLE_OPTIONS = [
  {
    label: 'User',
    value: 'User',
    description: 'Their own meetings and those shared with them, and every meeting source',
  },
  { label: 'Admin', value: 'Admin', description: 'Every meeting, the configuration and user management' },
  { label: 'Participant', value: 'Participant', description: 'Signs in to a home page only' },
];

const ROLE_BADGE_COLORS = { Admin: 'red', User: 'blue', Participant: 'grey' };

const EMAIL_REGEX = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

const formatError = (err) => {
  if (!err) return 'Unknown error';
  if (err.errors && err.errors.length) return err.errors.map((e) => e.message).join('; ');
  if (err.message) return err.message;
  return String(err);
};

const RoleBadge = ({ role = 'User' }) => <Badge color={ROLE_BADGE_COLORS[role] || 'blue'}>{role}</Badge>;
RoleBadge.propTypes = { role: PropTypes.string };

// What the Last sign-in and Last active columns show for a user with nothing
// recorded, and for every user when the times could not be read.
export const NOT_RECORDED_TEXT = 'Not recorded';
export const UNAVAILABLE_TEXT = 'Unavailable';

export const lastSignInText = (item, lastSignInAvailable) => {
  if (!lastSignInAvailable) return UNAVAILABLE_TEXT;
  return item.lastSignInAt ? new Date(item.lastSignInAt).toLocaleString() : NOT_RECORDED_TEXT;
};

// The latest sign-in, token renewal or MCP API key use, recorded to within 15 minutes.
export const lastActiveText = (item, lastSignInAvailable) => {
  if (!lastSignInAvailable) return UNAVAILABLE_TEXT;
  return item.lastActiveAt ? new Date(item.lastActiveAt).toLocaleString() : NOT_RECORDED_TEXT;
};

/**
 * The display name and short names fields, shared by Create user and Edit
 * names: the names an account goes by in meetings, by which a meeting's
 * attendees are matched to its participant list for their consent given in
 * advance.
 */
export const NamesFields = ({ displayName, shortNames, onDisplayName, onShortNames, accountName = '' }) => {
  const shortProblem = shortNamesProblem(displayName, parseShortNames(shortNames));
  return (
    <>
      <FormField
        label={
          <span>
            Display name in meetings <i>- optional</i>
          </span>
        }
        description="The name the person usually shows in meetings. LMA matches attendees to participant lists by it."
        constraintText={
          accountName && !displayName.trim()
            ? `Without one, the account's name applies: ${accountName}`
            : 'At most 100 characters.'
        }
        errorText={displayNameProblem(displayName) || undefined}
      >
        <Input
          value={displayName}
          onChange={({ detail }) => onDisplayName(detail.value)}
          placeholder={accountName || 'For example, Ana Silva'}
          ariaLabel="Display name in meetings"
        />
      </FormField>
      <FormField
        label={
          <span>
            Short names <i>- optional</i>
          </span>
        }
        description="Other names the person shows in meetings, separated by commas."
        constraintText="At most 5, each at most 50 characters."
        errorText={shortProblem || undefined}
      >
        <Input
          value={shortNames}
          onChange={({ detail }) => onShortNames(detail.value)}
          placeholder="For example, Ana, A. Silva"
          ariaLabel="Short names"
        />
      </FormField>
    </>
  );
};

NamesFields.propTypes = {
  displayName: PropTypes.string.isRequired,
  shortNames: PropTypes.string.isRequired,
  onDisplayName: PropTypes.func.isRequired,
  onShortNames: PropTypes.func.isRequired,
  accountName: PropTypes.string,
};

const columnDefinitions = (lastSignInAvailable) => [
  {
    id: 'email',
    header: 'Email',
    cell: (item) => item.email || item.username,
    sortingField: 'email',
    isRowHeader: true,
  },
  {
    id: 'names',
    header: 'Names in meetings',
    cell: (item) => namesSummary(item.displayName, item.shortNames, item.accountName),
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
  {
    id: 'lastSignInAt',
    header: 'Last sign-in',
    cell: (item) => lastSignInText(item, lastSignInAvailable),
    sortingField: 'lastSignInAt',
  },
  {
    id: 'lastActiveAt',
    header: 'Last active',
    cell: (item) => lastActiveText(item, lastSignInAvailable),
    sortingField: 'lastActiveAt',
  },
];

export const OWN_ROLE_REASON = "You can't change your own role";
export const LAST_ADMIN_REASON = 'At least one Admin is required';

// Why Change role is unavailable for this user, or null when it is available.
// The setUserRole resolver applies the same two rules; this only keeps the UI
// from offering a change it would refuse.
export const changeRoleBlockedReason = (target, users, signedInUsername) => {
  if (!target) return null;
  const own = (signedInUsername || '').toLowerCase();
  if (own && (target.username || '').toLowerCase() === own) return OWN_ROLE_REASON;
  if (target.role === 'Admin' && users.filter((u) => u.role === 'Admin').length <= 1) return LAST_ADMIN_REASON;
  return null;
};

const UserManagementPage = () => {
  const { user: signedInUser } = useAppContext() || {};
  const [users, setUsers] = useState([]);
  const [lastSignInAvailable, setLastSignInAvailable] = useState(true);
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
  // "Meeting to invite them to": what was chosen, typed or pasted.
  const [inviteMeeting, setInviteMeeting] = useState('');
  const [meetingOptions, setMeetingOptions] = useState([]);
  const [meetingOptionsStatus, setMeetingOptionsStatus] = useState('pending');

  const [roleOpen, setRoleOpen] = useState(false);
  const [roleChoice, setRoleChoice] = useState(ROLE_OPTIONS[0]);
  const [roleBusy, setRoleBusy] = useState(false);
  const [roleErr, setRoleErr] = useState(null);

  const [newDisplayName, setNewDisplayName] = useState('');
  const [newShortNames, setNewShortNames] = useState('');

  const [namesOpen, setNamesOpen] = useState(false);
  const [namesDisplay, setNamesDisplay] = useState('');
  const [namesShort, setNamesShort] = useState('');
  const [namesBusy, setNamesBusy] = useState(false);
  const [namesErr, setNamesErr] = useState(null);

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
      // Only an explicit true shows the times; anything else shows Unavailable.
      setLastSignInAvailable(result?.data?.listUsers?.lastSignInAvailable === true);
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

  const tableColumns = useMemo(() => columnDefinitions(lastSignInAvailable), [lastSignInAvailable]);

  const filteredUsers = useMemo(() => {
    if (!filterText) return users;
    const q = filterText.toLowerCase();
    return users.filter(
      (u) =>
        (u.email || '').toLowerCase().includes(q) ||
        (u.username || '').toLowerCase().includes(q) ||
        (u.role || '').toLowerCase().includes(q) ||
        (u.status || '').toLowerCase().includes(q) ||
        namesSummary(u.displayName, u.shortNames, u.accountName).toLowerCase().includes(q),
    );
  }, [users, filterText]);

  // The meetings the form offers, read when it opens; a paste works without them.
  const loadInvitableMeetings = async () => {
    setMeetingOptionsStatus('loading');
    try {
      const end = new Date();
      const start = new Date(end.getTime() - INVITABLE_MEETING_DAYS * 86400000);
      const result = await client.graphql({
        query: listInvitableMeetings,
        variables: { startDateTime: start.toISOString(), endDateTime: end.toISOString() },
      });
      setMeetingOptions(
        invitableMeetingOptions(result?.data?.listVirtualParticipants, result?.data?.listCallsDateRange?.Calls),
      );
      setMeetingOptionsStatus('finished');
    } catch (err) {
      logger.error('listing the meetings to invite to failed', err);
      setMeetingOptionsStatus('error');
    }
  };

  const handleOpenCreate = () => {
    setNewEmail('');
    setNewRole(ROLE_OPTIONS[0]);
    setNewDisplayName('');
    setNewShortNames('');
    setInviteMeeting('');
    setCreateErr(null);
    setCreateOpen(true);
    loadInvitableMeetings();
  };

  const meetingRef = meetingRefFromText(inviteMeeting);
  const chosenMeeting = meetingRef
    ? meetingOptions.flatMap((group) => group.options).find((option) => option.value === meetingRef)
    : null;

  const handleCreate = async () => {
    setCreateErr(null);
    const email = (newEmail || '').trim();
    if (!EMAIL_REGEX.test(email)) {
      setCreateErr('Please enter a valid email address.');
      return;
    }
    const names = namesInput(newDisplayName, newShortNames);
    const problem = namesProblem(names.displayName, names.shortNames);
    if (problem) {
      setCreateErr(problem);
      return;
    }
    const hasNames = Boolean(names.displayName || names.shortNames.length);
    setCreateBusy(true);
    try {
      await client.graphql({
        query: createUser,
        variables: {
          input: {
            email,
            role: newRole.value,
            ...(meetingRef ? { inviteToMeeting: meetingRef } : {}),
            ...(hasNames ? names : {}),
          },
        },
      });
      setSuccess(
        meetingRef
          ? `User ${email} created. An invitation email with a temporary password and the meeting's consent link ` +
              "has been sent, and the address is on the meeting's participant list."
          : `User ${email} created. An invitation email with a temporary password has been sent.`,
      );
      setCreateOpen(false);
      await fetchUsers();
    } catch (err) {
      logger.error('createUser failed', err);
      setCreateErr(formatError(err));
    } finally {
      setCreateBusy(false);
    }
  };

  const roleTarget = selected.length === 1 ? selected[0] : null;
  const roleBlockedReason = changeRoleBlockedReason(roleTarget, users, signedInUser?.username);

  const handleOpenRole = () => {
    if (!roleTarget || roleBlockedReason) return;
    setRoleChoice(ROLE_OPTIONS.find((option) => option.value === roleTarget.role) || ROLE_OPTIONS[0]);
    setRoleErr(null);
    setRoleOpen(true);
  };

  const handleChangeRole = async () => {
    if (!roleTarget) return;
    setRoleBusy(true);
    setRoleErr(null);
    try {
      await client.graphql({
        query: setUserRole,
        variables: { input: { username: roleTarget.username, role: roleChoice.value } },
      });
      setSuccess(
        `${roleTarget.email || roleTarget.username} is now ${roleChoice.value === 'Admin' ? 'an' : 'a'} ` +
          `${roleChoice.value}. The change applies at their next sign-in or within an hour.`,
      );
      setRoleOpen(false);
      setSelected([]);
      await fetchUsers();
    } catch (err) {
      logger.error('setUserRole failed', err);
      setRoleErr(formatError(err));
    } finally {
      setRoleBusy(false);
    }
  };

  const namesTarget = selected.length === 1 ? selected[0] : null;

  const handleOpenNames = () => {
    if (!namesTarget) return;
    setNamesDisplay(namesTarget.displayName || '');
    setNamesShort(shortNamesText(namesTarget.shortNames));
    setNamesErr(null);
    setNamesOpen(true);
  };

  const handleSaveNames = async () => {
    if (!namesTarget) return;
    const names = namesInput(namesDisplay, namesShort);
    setNamesBusy(true);
    setNamesErr(null);
    try {
      await client.graphql({
        query: setUserAccountNames,
        variables: { input: { username: namesTarget.username, ...names } },
      });
      setSuccess(`The names in meetings of ${namesTarget.email || namesTarget.username} are saved.`);
      setNamesOpen(false);
      await fetchUsers();
    } catch (err) {
      logger.error('setUserAccountNames failed', err);
      setNamesErr(formatError(err));
    } finally {
      setNamesBusy(false);
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
            description="Create and delete users and choose their roles (Admin only). New users get an invitation."
            counter={`(${users.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={fetchUsers} loading={loading}>
                  Refresh
                </Button>
                <Button
                  disabled={!roleTarget || Boolean(roleBlockedReason)}
                  disabledReason={roleBlockedReason || undefined}
                  onClick={handleOpenRole}
                >
                  Change role
                </Button>
                <Button
                  disabled={!namesTarget}
                  disabledReason={selected.length > 1 ? 'Select one user' : 'Select a user'}
                  onClick={handleOpenNames}
                >
                  Edit names
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
          columnDefinitions={tableColumns}
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
          <NamesFields
            displayName={newDisplayName}
            shortNames={newShortNames}
            onDisplayName={setNewDisplayName}
            onShortNames={setNewShortNames}
          />
          <FormField
            label={
              <span>
                Meeting to invite them to <i>- optional</i>
              </span>
            }
            description={
              'The invitation then names the meeting and links to its consent page, and the address is added to ' +
              "the meeting's participant list. Choose a Virtual Participant or a recent meeting, or paste a meeting " +
              'or Virtual Participant id or link.'
            }
            constraintText={chosenMeeting ? `Invites them to ${chosenMeeting.label}` : undefined}
          >
            <Autosuggest
              value={inviteMeeting}
              onChange={({ detail }) => setInviteMeeting(detail.value)}
              options={meetingOptions}
              statusType={meetingOptionsStatus}
              loadingText="Loading meetings"
              errorText="The meetings could not be listed; paste an id instead."
              empty="No meetings to choose from; paste an id instead."
              enteredTextLabel={(value) => `Use: "${meetingRefFromText(value)}"`}
              placeholder="No meeting"
              ariaLabel="Meeting to invite them to"
              filteringType="auto"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Change Role Modal */}
      <Modal
        visible={roleOpen}
        onDismiss={() => setRoleOpen(false)}
        header="Change role"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setRoleOpen(false)} disabled={roleBusy}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleChangeRole}
                loading={roleBusy}
                disabled={!roleTarget || roleChoice.value === roleTarget.role}
              >
                Change role
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {roleErr && <Alert type="error">{roleErr}</Alert>}
          <Box variant="p">
            Choose the role of <strong>{roleTarget?.email || roleTarget?.username}</strong>. The change applies at their
            next sign-in or token refresh, within an hour.
          </Box>
          <FormField label="Role">
            <Select
              selectedOption={roleChoice}
              options={ROLE_OPTIONS}
              onChange={({ detail }) => setRoleChoice(detail.selectedOption)}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Edit Names Modal */}
      <Modal
        visible={namesOpen}
        onDismiss={() => setNamesOpen(false)}
        header="Edit names in meetings"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setNamesOpen(false)} disabled={namesBusy}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSaveNames}
                loading={namesBusy}
                disabled={Boolean(namesProblem(namesDisplay, parseShortNames(namesShort)))}
              >
                Save
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {namesErr && <Alert type="error">{namesErr}</Alert>}
          <Box variant="p">
            The names <strong>{namesTarget?.email || namesTarget?.username}</strong> goes by in meetings. LMA matches a
            meeting&apos;s attendees to its participant list by them, for consent given in advance. The person can also
            change them on their My consents page.
          </Box>
          <NamesFields
            displayName={namesDisplay}
            shortNames={namesShort}
            onDisplayName={setNamesDisplay}
            onShortNames={setNamesShort}
            accountName={namesTarget?.accountName || ''}
          />
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
