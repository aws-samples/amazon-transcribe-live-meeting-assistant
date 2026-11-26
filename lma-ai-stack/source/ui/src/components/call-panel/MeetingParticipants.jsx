/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Autosuggest,
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

import {
  createParticipantAccounts,
  getMeetingParticipantAccounts,
  searchAccounts,
} from '../../graphql/queries/meetingParticipants';
import { appBase, consentInvitation, consentLink } from '../common/advance-consent';
import {
  MAX_PARTICIPANTS,
  MIN_ACCOUNT_SEARCH_CHARS,
  accountCreationSummary,
  accountLabel,
  parseParticipantsText,
  participantsInput,
  participantsToText,
  searchableText,
  withParticipant,
} from '../common/participant-list';

const client = generateClient();
const logger = new ConsoleLogger('MeetingParticipants');

/** The LMA account column's content for one address: "No account", or the account's role. */
const accountCellContent = (accounts, accountsError, email) => {
  if (!accounts) return accountsError ? '-' : <StatusIndicator type="loading">Checking</StatusIndicator>;
  const account = accounts.get(email.toLowerCase());
  // "unknown": the lookup was throttled; reading the list again checks it again.
  const type = { none: 'stopped', unknown: 'warning' }[account] || 'success';
  return <StatusIndicator type={type}>{accountLabel(account)}</StatusIndicator>;
};

const errorText = (error, fallback) => error?.errors?.[0]?.message || error?.message || fallback;

/** How long the picker waits after the last key before it searches. */
const SEARCH_DELAY_MS = 300;

/**
 * Find an LMA account by the start of its display name, short name or email
 * address, and add the person to the list being edited. The server answers at
 * most 10 accounts for at least 3 characters, to the meeting's owner and
 * Admins only; `listed` holds the addresses already on the list, in lower case.
 */
export const AccountPicker = ({ listed, onPick }) => {
  const [value, setValue] = useState('');
  const [options, setOptions] = useState([]);
  const [status, setStatus] = useState('finished');
  const [error, setError] = useState('');
  const latest = useRef('');
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const search = async (typed) => {
    try {
      const response = await client.graphql({ query: searchAccounts, variables: { text: typed } });
      if (latest.current !== typed) return;
      const found = response?.data?.searchAccounts || [];
      setOptions(
        found.map((account) => ({
          value: account.email,
          label: account.name || account.email,
          description: account.name ? account.email : undefined,
          disabled: listed.has(account.email.toLowerCase()),
          tags: listed.has(account.email.toLowerCase()) ? ['On the list'] : undefined,
          name: account.name || '',
        })),
      );
      setStatus('finished');
    } catch (e) {
      if (latest.current !== typed) return;
      logger.error('could not search the accounts', e);
      setError(errorText(e, 'The accounts could not be searched.'));
      setStatus('error');
    }
  };

  const load = (text) => {
    const typed = (text || '').trim();
    latest.current = typed;
    clearTimeout(timer.current);
    if (!searchableText(typed)) {
      setOptions([]);
      setStatus('finished');
      return;
    }
    setStatus('loading');
    timer.current = setTimeout(() => search(typed), SEARCH_DELAY_MS);
  };

  const pick = (email) => {
    const option = options.find((o) => o.value === email);
    if (!option || option.disabled) return;
    onPick({ name: option.name, email: option.value });
    setValue('');
    setOptions([]);
    latest.current = '';
  };

  return (
    <FormField
      label="Add a person with an LMA account"
      description={
        `Type at least ${MIN_ACCOUNT_SEARCH_CHARS} characters of the start of their name or email address. ` +
        'The person is added below as Name <email>. The names their account goes by in meetings also count ' +
        'when LMA matches attendees to the list.'
      }
    >
      <Autosuggest
        value={value}
        onChange={({ detail }) => setValue(detail.value)}
        onLoadItems={({ detail }) => load(detail.filteringText)}
        onSelect={({ detail }) => pick(detail.value)}
        options={options}
        filteringType="manual"
        statusType={status}
        loadingText="Searching accounts"
        errorText={error}
        recoveryText="Retry"
        empty={searchableText(value) ? 'No account found' : `Type at least ${MIN_ACCOUNT_SEARCH_CHARS} characters`}
        enteredTextLabel={(text) => `Search for "${text}"`}
        placeholder="Name or email address"
        ariaLabel="Find an LMA account to add"
      />
    </FormField>
  );
};

AccountPicker.propTypes = {
  listed: PropTypes.instanceOf(Set).isRequired,
  onPick: PropTypes.func.isRequired,
};

/**
 * The invitation to consent in advance: one email to everyone on the list,
 * from the organizer's own mail client or copied, with one link to the
 * meeting's consent page. The page requires sign-in and checks the address
 * signed in with is on the list, so one link serves everyone.
 */
export const ConsentInvitationModal = ({ visible, onDismiss, invitation }) => {
  const [copied, setCopied] = useState('');
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(invitation.text);
      setCopied('success');
    } catch (error) {
      logger.error('could not copy the invitation', error);
      setCopied('error');
    }
  };
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Invite participants to consent"
      size="large"
      footer={
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button variant="link" onClick={onDismiss}>
              Close
            </Button>
            <Button onClick={copy} iconName="copy">
              Copy text
            </Button>
            <Button variant="primary" href={invitation.mailto} iconName="envelope">
              Open in mail app
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          One email to everyone on the list, sent from your own mail client, with one link to this meeting&apos;s
          consent page. Each person signs in to LMA with the address it was sent to; anyone not on the list is told so
          and can ask you to add them. LMA does not send this email.
        </Box>
        {invitation.shortened && (
          <Alert type="info">
            The list is too long for a mail link, so the mail app opens without recipients. Use Copy text, or add the
            recipients below yourself.
          </Alert>
        )}
        {copied === 'success' && <StatusIndicator type="success">Copied</StatusIndicator>}
        {copied === 'error' && <StatusIndicator type="error">Could not copy; select the text below</StatusIndicator>}
        <FormField label="To">
          <Box>{invitation.to.join(', ')}</Box>
        </FormField>
        <FormField label="Subject">
          <Box>{invitation.subject}</Box>
        </FormField>
        <FormField label="Text" stretch>
          <Textarea value={invitation.body} readOnly rows={14} ariaLabel="Invitation text" />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

ConsentInvitationModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  invitation: PropTypes.shape({
    to: PropTypes.arrayOf(PropTypes.string),
    subject: PropTypes.string,
    body: PropTypes.string,
    text: PropTypes.string,
    mailto: PropTypes.string,
    shortened: PropTypes.bool,
  }).isRequired,
};

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
  isOwner = false,
  meetingName = '',
  owner = '',
  scheduledFor = '',
  requests = [],
  onSave = undefined,
  description = '',
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
  const [inviting, setInviting] = useState(false);

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
  // Requests from people who have been added since are answered.
  const listedAddresses = new Set(people.map((p) => p.email.toLowerCase()));
  const openRequests = (requests || []).filter((r) => r?.email && !listedAddresses.has(r.email.toLowerCase()));

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

  const startEditing = () => {
    setDraft(participantsToText(people));
    setSaveError('');
    setEditing(true);
  };
  // "Add participants" while the list is empty, so the way to start one is plain.
  const editLabel = people.length ? 'Edit' : 'Add participants';

  const actions = canManage ? (
    <SpaceBetween size="xs" direction="horizontal">
      {canEdit && !editing && (
        <Button iconName={people.length ? 'edit' : 'add-plus'} onClick={startEditing}>
          {editLabel}
        </Button>
      )}
      {isOwner && people.length > 0 && hasRef && (
        <Button iconName="envelope" onClick={() => setInviting(true)}>
          Invite participants to consent
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
          description={[
            "The people expected at the meeting. With the deployment's Meeting Sharing setting on (the default), " +
              'the meeting is shared, read-only, with each person listed.',
            canEdit ? 'The list can be changed at any time, before, during and after the meeting.' : '',
            description,
          ]
            .filter(Boolean)
            .join(' ')}
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
        {canManage && openRequests.length > 0 && (
          <Alert type="info" header="Asked to be added">
            {`${openRequests.length === 1 ? 'This person' : 'These people'} opened the meeting's consent link and ` +
              `${openRequests.length === 1 ? 'is' : 'are'} not on the list: ` +
              `${openRequests.map((r) => (r.name ? `${r.name} <${r.email}>` : r.email)).join(', ')}. ` +
              'Add them to the list to let them give consent in advance.'}
          </Alert>
        )}
        {editing ? (
          <SpaceBetween size="s">
            <AccountPicker
              listed={new Set(parsed.participants.map((p) => p.email.toLowerCase()))}
              onPick={(person) => setDraft((text) => withParticipant(text, person))}
            />
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
            empty={
              <SpaceBetween size="xs" alignItems="center">
                <Box color="text-body-secondary">No participants listed.</Box>
                {canEdit && (
                  <Button iconName="add-plus" onClick={startEditing} ariaLabel="Add participants to the list">
                    Add participants
                  </Button>
                )}
              </SpaceBetween>
            }
          />
        )}
      </SpaceBetween>
      {inviting && (
        <ConsentInvitationModal
          visible={inviting}
          onDismiss={() => setInviting(false)}
          invitation={consentInvitation({
            meetingName: meetingName || callId,
            organizer: owner,
            scheduledFor,
            participants: people,
            link: consentLink(appBase(), { callId, virtualParticipantId }),
          })}
        />
      )}
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
  // The meeting's owner, who alone sends the invitation to consent, from
  // their own mail client.
  isOwner: PropTypes.bool,
  meetingName: PropTypes.string,
  owner: PropTypes.string,
  scheduledFor: PropTypes.string,
  // People not on the list who asked to be added (a Virtual Participant's).
  requests: PropTypes.arrayOf(PropTypes.shape({ name: PropTypes.string, email: PropTypes.string })),
  onSave: PropTypes.func,
  // Added to the section's description: which list it shows, on a Virtual
  // Participant's page.
  description: PropTypes.string,
};

export default MeetingParticipants;
