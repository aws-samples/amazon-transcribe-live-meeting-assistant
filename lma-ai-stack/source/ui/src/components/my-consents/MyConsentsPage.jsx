/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { useLocation } from 'react-router-dom';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  ContentLayout,
  DatePicker,
  ExpandableSection,
  FormField,
  Header,
  Input,
  Modal,
  RadioGroup,
  Select,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';

import {
  askToBeAddedToMeeting,
  getAdvanceConsentInvitation,
  giveAdvanceConsent,
  listMyAdvanceConsents,
  withdrawAdvanceConsent,
} from '../../graphql/queries/advanceConsent';
import {
  consentLinkMeeting,
  consentScopeLabel,
  consentStatusLabel,
  dateAhead,
  localDay,
} from '../common/advance-consent';

const client = generateClient();
const logger = new ConsoleLogger('MyConsentsPage');

const errorText = (error, fallback) => error?.errors?.[0]?.message || error?.message || fallback;
const when = (iso) => (iso ? new Date(iso).toLocaleString() : '-');
/** How far ahead a standing consent runs unless the person picks another date. */
const DEFAULT_STANDING_DAYS = 90;

/**
 * A meeting's consent page, opened from its consent link: whether the
 * signed-in person is on its participant list and, when they are, the consent
 * they can give and the exact text they would agree to, read from the server
 * for each choice. Someone not on the list can only ask to be added.
 */
export const MeetingConsentInvitation = ({ meetingRef, onGiven }) => {
  const [scope, setScope] = useState('meeting');
  const [until, setUntil] = useState('');
  const [labelKey, setLabelKey] = useState('');
  const [invitation, setInvitation] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [agreed, setAgreed] = useState(false);
  const [giving, setGiving] = useState(false);
  const [result, setResult] = useState(null);
  const [requestName, setRequestName] = useState('');
  const [asking, setAsking] = useState(false);
  const [asked, setAsked] = useState(false);

  const load = useCallback(async () => {
    const variables = { ...meetingRef, Scope: scope };
    if (scope === 'organizer') {
      variables.ValidUntil = until || undefined;
      variables.LabelKey = labelKey || undefined;
    }
    try {
      const response = await client.graphql({ query: getAdvanceConsentInvitation, variables });
      const answer = response?.data?.getAdvanceConsentInvitation;
      setInvitation(answer);
      setLoadError('');
    } catch (error) {
      logger.error('could not read the consent page', error);
      setLoadError(errorText(error, 'The consent page could not be read.'));
    }
  }, [JSON.stringify(meetingRef), scope, until, labelKey]);

  useEffect(() => {
    // The text agreed to changes with the choice, so agreeing is asked again.
    setAgreed(false);
    load();
  }, [load]);

  if (loadError && !invitation) return <Alert type="error">{loadError}</Alert>;
  if (!invitation) return <StatusIndicator type="loading">Loading the meeting&apos;s consent page</StatusIndicator>;

  if (!invitation.Listed) {
    const requested = asked || invitation.Requested;
    const ask = async () => {
      setAsking(true);
      try {
        await client.graphql({
          query: askToBeAddedToMeeting,
          variables: { input: { ...meetingRef, Name: requestName || undefined } },
        });
        setAsked(true);
      } catch (error) {
        logger.error('could not ask to be added', error);
        setLoadError(errorText(error, 'The request could not be sent.'));
      } finally {
        setAsking(false);
      }
    };
    return (
      <Container header={<Header variant="h2">You are not on this meeting&apos;s list</Header>}>
        <SpaceBetween size="s">
          <Box>
            {`The meeting's participant list does not include ${invitation.Email || 'your email address'}, ` +
              'so you cannot give consent for it here. Nothing has been recorded about you.'}
          </Box>
          {loadError && <Alert type="error">{loadError}</Alert>}
          {requested ? (
            <Alert type="success">
              You asked to be added. The organizer sees your request in LMA; once you are added to the list, open this
              link again to give your consent.
            </Alert>
          ) : (
            invitation.Enabled && (
              <SpaceBetween size="s">
                <FormField
                  label="Your name (optional)"
                  description={
                    'Your name and email address are shown to the people who can see the meeting in LMA: its ' +
                    'organizer, the people it is shared with, and administrators.'
                  }
                >
                  <Input value={requestName} onChange={({ detail }) => setRequestName(detail.value)} />
                </FormField>
                <Button onClick={ask} loading={asking}>
                  Ask to be added
                </Button>
              </SpaceBetween>
            )
          )}
        </SpaceBetween>
      </Container>
    );
  }

  if (!invitation.Enabled) {
    return <Alert type="info">This deployment does not take consent given in advance.</Alert>;
  }

  const labelOptions = [
    { value: '', label: 'Every meeting they organize' },
    ...(invitation.Labels || []).map((l) => ({ value: l.Key, label: `Only meetings labelled ${l.Key}: ${l.Value}` })),
  ];

  const give = async () => {
    setGiving(true);
    setResult(null);
    const input = { ...meetingRef, Scope: scope, NoticeHash: invitation.NoticeHash };
    if (scope === 'organizer') {
      input.ValidUntil = until;
      if (labelKey) input.LabelKey = labelKey;
    }
    try {
      await client.graphql({ query: giveAdvanceConsent, variables: { input } });
      setResult({ type: 'success', text: 'Your consent is recorded. You can withdraw it at any time below.' });
      setAgreed(false);
      onGiven();
      load();
    } catch (error) {
      logger.error('could not give consent', error);
      setResult({ type: 'error', text: errorText(error, 'Your consent could not be recorded.') });
      load();
    } finally {
      setGiving(false);
    }
  };

  const active = invitation.ActiveConsents || [];
  return (
    <Container
      header={
        <Header
          variant="h2"
          description={`Organized by ${invitation.Organizer}${
            invitation.ScheduledFor ? `, ${when(invitation.ScheduledFor)}` : ''
          }. Listed as ${invitation.ListedName || invitation.Email}.`}
        >
          {`Consent for ${invitation.MeetingName}`}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {active.length > 0 && (
          <Alert type="info">
            {`You already have consent in force for this meeting: ${active.map(consentScopeLabel).join('; ')}.`}
          </Alert>
        )}
        {result && (
          <Alert type={result.type} dismissible onDismiss={() => setResult(null)}>
            {result.text}
          </Alert>
        )}
        {loadError && <Alert type="error">{loadError}</Alert>}
        <FormField label="What your consent covers">
          <RadioGroup
            value={scope}
            onChange={({ detail }) => {
              // A standing consent always names its date, so one is chosen for it.
              if (detail.value === 'organizer' && !until) {
                setUntil(dateAhead(Math.min(DEFAULT_STANDING_DAYS, invitation.MaxDays || DEFAULT_STANDING_DAYS)));
              }
              setScope(detail.value);
            }}
            items={[
              { value: 'meeting', label: 'This meeting only' },
              {
                value: 'organizer',
                label: `Meetings organized by ${invitation.Organizer}, until a date`,
                description: `At most ${invitation.MaxDays} days ahead.`,
              },
            ]}
          />
        </FormField>
        {scope === 'organizer' && (
          <SpaceBetween size="s" direction="horizontal">
            <FormField label="Until (UTC)" constraintText="YYYY/MM/DD">
              <DatePicker
                value={until}
                onChange={({ detail }) => setUntil(detail.value)}
                isDateEnabled={(date) => {
                  const day = localDay(date);
                  return day >= dateAhead(0) && day <= dateAhead(invitation.MaxDays || 365);
                }}
                placeholder="YYYY/MM/DD"
                openCalendarAriaLabel={(selected) => `Choose the date${selected ? `, ${selected}` : ''}`}
              />
            </FormField>
            {labelOptions.length > 1 && (
              <FormField label="Which meetings">
                <Select
                  selectedOption={labelOptions.find((o) => o.value === labelKey) || labelOptions[0]}
                  onChange={({ detail }) => setLabelKey(detail.selectedOption.value)}
                  options={labelOptions}
                />
              </FormField>
            )}
          </SpaceBetween>
        )}
        <FormField label="You agree to">
          <Box variant="p" data-testid="advance-consent-text">
            {invitation.NoticeText}
          </Box>
        </FormField>
        <Checkbox checked={agreed} onChange={({ detail }) => setAgreed(detail.checked)}>
          I have read this text and agree to it
        </Checkbox>
        <Box color="text-body-secondary" fontSize="body-s">
          The record keeps your email address, your account, the time and this text. You can still object during any
          meeting, and withdraw this consent at any time on this page.
        </Box>
        <Button variant="primary" onClick={give} loading={giving} disabled={!agreed || !invitation.NoticeHash}>
          Give consent
        </Button>
      </SpaceBetween>
    </Container>
  );
};

MeetingConsentInvitation.propTypes = {
  meetingRef: PropTypes.shape({ CallId: PropTypes.string, VirtualParticipantId: PropTypes.string }).isRequired,
  onGiven: PropTypes.func.isRequired,
};

const statusCell = (c) => (
  <StatusIndicator type={c.Status === 'active' ? 'success' : 'stopped'}>
    {`${consentStatusLabel(c)}${c.WithdrawnAt ? ` ${when(c.WithdrawnAt)}` : ''}`}
  </StatusIndicator>
);

const textCell = (c) => (
  <ExpandableSection headerText="Show">
    <SpaceBetween size="xxs">
      <Box variant="p">{c.NoticeText}</Box>
      <Box color="text-body-secondary" fontSize="body-s">
        {`Version ${c.NoticeVersion}; SHA-256 ${c.NoticeHash}${
          c.DeviceDetailsKept ? '; sign-in device details kept' : ''
        }`}
      </Box>
    </SpaceBetween>
  </ExpandableSection>
);

const withdrawCell = (c, onWithdraw) =>
  c.Status === 'active' ? (
    <Button variant="inline-link" onClick={() => onWithdraw(c)} ariaLabel={`Withdraw ${consentScopeLabel(c)}`}>
      Withdraw
    </Button>
  ) : null;

/** The person's own consents given in advance, with withdrawal. */
export const MyConsentsList = ({ answer = null, error = '', onWithdrawn }) => {
  const [confirming, setConfirming] = useState(null);
  const [withdrawing, setWithdrawing] = useState(false);
  const [withdrawError, setWithdrawError] = useState('');

  const withdraw = async () => {
    setWithdrawing(true);
    setWithdrawError('');
    try {
      await client.graphql({
        query: withdrawAdvanceConsent,
        variables: { input: { ConsentId: confirming.ConsentId } },
      });
      setConfirming(null);
      onWithdrawn();
    } catch (e) {
      logger.error('could not withdraw the consent', e);
      setWithdrawError(errorText(e, 'The consent could not be withdrawn.'));
    } finally {
      setWithdrawing(false);
    }
  };

  const columns = [
    { id: 'scope', header: 'What it covers', cell: (c) => consentScopeLabel(c) },
    { id: 'given', header: 'Given', cell: (c) => when(c.GrantedAt) },
    { id: 'until', header: 'Counts until', cell: (c) => when(c.ValidUntil) },
    { id: 'status', header: 'Status', cell: (c) => statusCell(c) },
    { id: 'text', header: 'Text agreed to', cell: (c) => textCell(c) },
    { id: 'actions', header: '', cell: (c) => withdrawCell(c, setConfirming) },
  ];

  return (
    <>
      <Table
        header={
          <Header
            variant="h2"
            counter={answer?.Consents ? `(${answer.Consents.length})` : undefined}
            description={answer?.Email ? `Kept under ${answer.Email}.` : undefined}
          >
            Your consents
          </Header>
        }
        items={answer?.Consents || []}
        columnDefinitions={columns}
        trackBy="ConsentId"
        loading={!answer && !error}
        loadingText="Loading your consents"
        wrapLines
        empty={
          <Box color="text-body-secondary">
            {error ||
              'You have not given consent in advance. Open a meeting’s consent link from its organizer to give it.'}
          </Box>
        }
      />
      <Modal
        visible={Boolean(confirming)}
        onDismiss={() => setConfirming(null)}
        header="Withdraw this consent?"
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button variant="link" onClick={() => setConfirming(null)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={withdraw} loading={withdrawing}>
                Withdraw
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>{confirming ? consentScopeLabel(confirming) : ''}</Box>
          <Box>
            From now on it is no longer in force. A meeting&apos;s own notice and consent step are not affected. The
            record of what you agreed to, and when you withdrew, is kept.
          </Box>
          {withdrawError && <Alert type="error">{withdrawError}</Alert>}
        </SpaceBetween>
      </Modal>
    </>
  );
};

MyConsentsList.propTypes = {
  answer: PropTypes.shape({
    Email: PropTypes.string,
    Consents: PropTypes.arrayOf(PropTypes.shape({ ConsentId: PropTypes.string })),
  }),
  error: PropTypes.string,
  onWithdrawn: PropTypes.func.isRequired,
};

/**
 * My consents: the signed-in person's consents given in advance, whatever
 * their role, and, opened from a meeting's consent link, that meeting's
 * consent page above them.
 */
const MyConsentsPage = () => {
  const { search } = useLocation();
  const meetingRef = useMemo(() => consentLinkMeeting(search), [search]);
  const [answer, setAnswer] = useState(null);
  const [error, setError] = useState('');

  const loadConsents = useCallback(async () => {
    try {
      const response = await client.graphql({ query: listMyAdvanceConsents });
      setAnswer(response?.data?.listMyAdvanceConsents || { Consents: [] });
      setError('');
    } catch (e) {
      logger.error('could not read your consents', e);
      setError(errorText(e, 'Your consents could not be read.'));
    }
  }, []);

  useEffect(() => {
    loadConsents();
  }, [loadConsents]);

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={
            'Consent you give before a meeting to its being recorded and transcribed. ' +
            'You can still object during any meeting.'
          }
        >
          My consents
        </Header>
      }
    >
      <SpaceBetween size="l">
        {meetingRef && <MeetingConsentInvitation meetingRef={meetingRef} onGiven={loadConsents} />}
        <MyConsentsList answer={answer} error={error} onWithdrawn={loadConsents} />
      </SpaceBetween>
    </ContentLayout>
  );
};

export default MyConsentsPage;
