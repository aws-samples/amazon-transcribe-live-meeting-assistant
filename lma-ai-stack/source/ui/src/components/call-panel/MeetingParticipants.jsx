/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Box,
  Button,
  Container,
  FormField,
  Header,
  Modal,
  SpaceBetween,
  StatusIndicator,
  Table,
  Textarea,
} from '@cloudscape-design/components';

import { createParticipantAccounts, getMeetingParticipantAccounts } from '../../graphql/queries/meetingParticipants';
import {
  MAX_PARTICIPANTS,
  accountCreationSummary,
  accountLabel,
  parseParticipantsText,
  participantsInput,
  participantsToText,
} from '../common/participant-list';

const client = generateClient();
const logger = new ConsoleLogger('MeetingParticipants');

/** The LMA account column's content for one address: "No account", or the account's role. */
const accountCellContent = (accounts, accountsError, email) => {
  if (!accounts) return accountsError ? '-' : <StatusIndicator type="loading">Checking</StatusIndicator>;
  const account = accounts.get(email.toLowerCase());
  return <StatusIndicator type={account === 'none' ? 'stopped' : 'success'}>{accountLabel(account)}</StatusIndicator>;
};

const errorText = (error, fallback) => error?.errors?.[0]?.message || error?.message || fallback;

/**
 * The meeting's participant list: the people expected at it, from the Virtual
 * Participant form, an MCP client or this panel's editor.
 *
 * Everyone who can see the meeting sees the names and addresses. Its owner and
 * Admins (`canManage`) also see whether each person has an LMA account, can
 * create Participant accounts for the people who have none, and, given
 * `onSave`, change the list. The meeting is named by `callId`, or by
 * `virtualParticipantId` before a Virtual Participant has created it. See
 * docs/virtual-participant.md#participant-list.
 */
const MeetingParticipants = ({
  participants = [],
  callId = '',
  virtualParticipantId = '',
  canManage = false,
  onSave = undefined,
}) => {
  const people = participantsInput(participants);
  const listKey = JSON.stringify(people);
  const meetingRef = callId ? { CallId: callId } : { VirtualParticipantId: virtualParticipantId };
  const hasRef = Boolean(callId || virtualParticipantId);

  const [accounts, setAccounts] = useState(null);
  const [accountsError, setAccountsError] = useState('');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [creating, setCreating] = useState(false);
  const [summary, setSummary] = useState(null);

  const loadAccounts = useCallback(async () => {
    if (!canManage || !hasRef || !people.length) {
      setAccounts(null);
      return;
    }
    try {
      const response = await client.graphql({ query: getMeetingParticipantAccounts, variables: meetingRef });
      const answer = response?.data?.getMeetingParticipantAccounts || [];
      setAccounts(new Map(answer.map((entry) => [entry.email.toLowerCase(), entry.account])));
      setAccountsError('');
    } catch (error) {
      logger.error('could not read the participants’ accounts', error);
      setAccountsError(errorText(error, 'Whether each person has an account could not be read.'));
    }
    // listKey stands for `people`, which is a new array every render.
  }, [canManage, hasRef, callId, virtualParticipantId, listKey]);

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  const canEdit = canManage && Boolean(onSave);
  if (!people.length && !canEdit) return null;

  const accountOf = (email) => accounts?.get(email.toLowerCase());
  const withoutAccount = accounts ? people.filter((p) => accountOf(p.email) === 'none') : [];
  let createDisabledReason = 'Checking the accounts';
  if (accounts) createDisabledReason = 'Everyone on the list has an LMA account';
  else if (accountsError) createDisabledReason = 'The accounts could not be checked';

  const createAccounts = async () => {
    setConfirming(false);
    setCreating(true);
    setSummary(null);
    try {
      // Only the people the page knows have no account, and the answer updates
      // their status, so the list is not looked up again.
      const emails = withoutAccount.map((p) => p.email);
      const response = await client.graphql({
        query: createParticipantAccounts,
        variables: { input: { ...meetingRef, emails } },
      });
      const answer = response?.data?.createParticipantAccounts;
      setSummary(accountCreationSummary(answer));
      setAccounts((current) => {
        const next = new Map(current || []);
        (answer?.results || []).forEach((r) => {
          if (r.outcome === 'created') next.set(r.email.toLowerCase(), 'Participant');
        });
        return next;
      });
    } catch (error) {
      logger.error('could not create participant accounts', error);
      setSummary({ type: 'error', text: errorText(error, 'The accounts could not be created.') });
    } finally {
      setCreating(false);
    }
  };

  const parsed = editing ? parseParticipantsText(draft) : { participants: [], errors: [] };

  const save = async () => {
    setSaving(true);
    setSaveError('');
    try {
      const result = await onSave(parsed.participants);
      if (result?.ok) setEditing(false);
      else setSaveError(result?.message || 'The change could not be saved.');
    } finally {
      setSaving(false);
    }
  };

  const columns = [
    { id: 'name', header: 'Name', cell: (p) => p.name || '-' },
    { id: 'email', header: 'Email', cell: (p) => p.email },
  ];
  if (canManage) {
    columns.push({
      id: 'account',
      header: 'LMA account',
      cell: (p) => accountCellContent(accounts, accountsError, p.email),
    });
  }

  const actions = canManage ? (
    <SpaceBetween size="xs" direction="horizontal">
      {canEdit && (
        <Button
          iconName="edit"
          onClick={() => {
            setDraft(participantsToText(people));
            setSaveError('');
            setEditing(true);
          }}
        >
          Edit
        </Button>
      )}
      {people.length > 0 && hasRef && (
        <Button
          onClick={() => setConfirming(true)}
          loading={creating}
          disabled={!withoutAccount.length}
          disabledReason={createDisabledReason}
        >
          Create accounts for participants without one
        </Button>
      )}
    </SpaceBetween>
  ) : null;

  return (
    <Container
      header={
        <Header
          variant="h3"
          counter={people.length ? `(${people.length})` : undefined}
          description="The people expected at the meeting. Listing someone does not share the meeting with them."
          actions={actions}
        >
          Participants
        </Header>
      }
      data-testid="meeting-participants"
    >
      <SpaceBetween size="s">
        {summary && (
          <Alert type={summary.type} dismissible onDismiss={() => setSummary(null)}>
            {summary.text}
          </Alert>
        )}
        {accountsError && <Alert type="warning">{accountsError}</Alert>}
        {editing ? (
          <SpaceBetween size="s">
            <FormField
              label="Participants"
              description={`One per line as Name <email> or an email address; at most ${MAX_PARTICIPANTS}.`}
              errorText={parsed.errors.length ? parsed.errors.join('; ') : undefined}
              stretch
            >
              <Textarea
                value={draft}
                onChange={({ detail }) => setDraft(detail.value)}
                rows={Math.min(12, Math.max(3, draft.split('\n').length + 1))}
                ariaLabel="Participants, one Name <email> per line"
              />
            </FormField>
            {saveError && <StatusIndicator type="error">{saveError}</StatusIndicator>}
            <SpaceBetween size="xxs" direction="horizontal">
              <Button onClick={() => setEditing(false)} disabled={saving}>
                Cancel
              </Button>
              <Button variant="primary" onClick={save} loading={saving} disabled={parsed.errors.length > 0}>
                Save
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        ) : (
          <Table
            variant="embedded"
            items={people}
            columnDefinitions={columns}
            trackBy="email"
            empty={<Box color="text-body-secondary">No participants listed.</Box>}
          />
        )}
      </SpaceBetween>
      <Modal
        visible={confirming}
        onDismiss={() => setConfirming(false)}
        header="Create participant accounts"
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button variant="link" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={createAccounts}>
                Create accounts
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            {`An LMA account with the Participant role will be created for ${withoutAccount.length} ` +
              `${withoutAccount.length === 1 ? 'person' : 'people'} on this list who have none: ` +
              `${withoutAccount.map((p) => p.email).join(', ')}.`}
          </Box>
          <Box>
            Each is sent an email invitation with a temporary password. People who already have an account are not
            changed.
          </Box>
        </SpaceBetween>
      </Modal>
    </Container>
  );
};

MeetingParticipants.propTypes = {
  participants: PropTypes.arrayOf(PropTypes.shape({ name: PropTypes.string, email: PropTypes.string })),
  callId: PropTypes.string,
  virtualParticipantId: PropTypes.string,
  canManage: PropTypes.bool,
  onSave: PropTypes.func,
};

export default MeetingParticipants;
