/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { useLocation } from 'react-router-dom';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Autosuggest,
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
import { CONSENT_PROMPT_TEXT, dismissPrompt, sessionSub, shouldPrompt } from './consent-prompt';
import useAppContext from '../../contexts/app';

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
                    'organizer, the users it is shared with, and administrators.'
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

/** A typed address that is worth asking the server about. */
const looksLikeAddress = (value) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value.trim());

/**
 * A standing consent, given on My consents with no meeting link: for the
 * meetings one organizer holds (by the address the person types, never looked
 * up), optionally only those with one label, or for every meeting on the
 * deployment, until a date. The exact text is read from the server for each
 * choice, as on a meeting's consent page.
 */
export const StandingConsentForm = ({ answer = null, onGiven }) => {
  const maxDays = answer?.MaxDays || 365;
  const [scope, setScope] = useState('');
  const [until, setUntil] = useState(() => dateAhead(Math.min(DEFAULT_STANDING_DAYS, maxDays)));
  const [organizer, setOrganizer] = useState('');
  const [labelKey, setLabelKey] = useState('');
  const [labelValue, setLabelValue] = useState('');
  const [offer, setOffer] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [agreed, setAgreed] = useState(false);
  const [giving, setGiving] = useState(false);
  const [result, setResult] = useState(null);
  const latest = useRef(0);

  // The organizers named on the person's own consents, to pick from again.
  const known = useMemo(
    () =>
      [...new Set((answer?.Consents || []).map((c) => c.Organizer).filter(Boolean))].map((value) => ({
        value,
      })),
    [answer],
  );

  const choice = useMemo(() => {
    if (scope === 'deployment') return { Scope: scope, ValidUntil: until };
    if (scope !== 'organizer' || !looksLikeAddress(organizer)) return null;
    const out = { Scope: scope, ValidUntil: until, Organizer: organizer.trim() };
    if (labelKey.trim() && labelValue.trim()) {
      out.LabelKey = labelKey.trim();
      out.LabelValue = labelValue.trim();
    }
    return out;
  }, [scope, until, organizer, labelKey, labelValue]);

  const load = useCallback(async () => {
    setAgreed(false);
    if (!choice || !until) {
      setOffer(null);
      return;
    }
    latest.current += 1;
    const asked = latest.current;
    try {
      const response = await client.graphql({ query: getAdvanceConsentInvitation, variables: choice });
      if (asked !== latest.current) return;
      setOffer(response?.data?.getAdvanceConsentInvitation || null);
      setLoadError('');
    } catch (error) {
      if (asked !== latest.current) return;
      logger.error('could not read the consent text', error);
      setOffer(null);
      setLoadError(errorText(error, 'The consent text could not be read.'));
    }
  }, [JSON.stringify(choice)]);

  useEffect(() => {
    load();
  }, [load]);

  if (answer && answer.Enabled === false) {
    return <Alert type="info">This deployment does not take consent given in advance.</Alert>;
  }

  const give = async () => {
    setGiving(true);
    setResult(null);
    try {
      await client.graphql({
        query: giveAdvanceConsent,
        variables: { input: { ...choice, NoticeHash: offer.NoticeHash } },
      });
      setResult({ type: 'success', text: 'Your consent is recorded. You can withdraw it at any time below.' });
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

  const shown = choice && offer?.NoticeHash ? offer : null;
  const active = shown?.ActiveConsents || [];
  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Without a meeting's consent link, you can give a consent that runs until a date you choose."
        >
          Give a standing consent
        </Header>
      }
    >
      <SpaceBetween size="m">
        {result && (
          <Alert type={result.type} dismissible onDismiss={() => setResult(null)}>
            {result.text}
          </Alert>
        )}
        {loadError && <Alert type="error">{loadError}</Alert>}
        <FormField label="What your consent covers">
          <RadioGroup
            value={scope || null}
            onChange={({ detail }) => setScope(detail.value)}
            items={[
              {
                value: 'organizer',
                label: 'Meetings organized by one person, until a date',
                description: 'Their meetings on this deployment, named by their email address.',
              },
              {
                value: 'deployment',
                label: 'All meetings on this deployment, until a date',
                description: 'Every meeting whose participant list includes you.',
              },
            ]}
          />
        </FormField>
        {scope === 'organizer' && (
          <SpaceBetween size="s">
            <FormField
              label="Organizer's email address"
              description="The address they sign in to Live Meeting Assistant with."
            >
              <Autosuggest
                value={organizer}
                onChange={({ detail }) => setOrganizer(detail.value)}
                options={known}
                enteredTextLabel={(value) => `Use: ${value}`}
                placeholder="organizer@example.com"
                ariaLabel="Organizer's email address"
                empty="Type the organizer's email address"
              />
            </FormField>
            <SpaceBetween size="s" direction="horizontal">
              <FormField label="Only meetings with this label (optional)" description="Label name">
                <Input value={labelKey} onChange={({ detail }) => setLabelKey(detail.value)} ariaLabel="Label name" />
              </FormField>
              <FormField label="&nbsp;" description="Label value">
                <Input
                  value={labelValue}
                  onChange={({ detail }) => setLabelValue(detail.value)}
                  ariaLabel="Label value"
                />
              </FormField>
            </SpaceBetween>
          </SpaceBetween>
        )}
        {scope && (
          <FormField label="Until (UTC)" constraintText={`YYYY/MM/DD, at most ${maxDays} days ahead.`}>
            <DatePicker
              value={until}
              onChange={({ detail }) => setUntil(detail.value)}
              isDateEnabled={(date) => {
                const day = localDay(date);
                return day >= dateAhead(0) && day <= dateAhead(maxDays);
              }}
              placeholder="YYYY/MM/DD"
              openCalendarAriaLabel={(selected) => `Choose the date${selected ? `, ${selected}` : ''}`}
            />
          </FormField>
        )}
        {active.length > 0 && (
          <Alert type="info">
            {`You already have this consent in force: ${active.map(consentScopeLabel).join('; ')}. ` +
              'Giving it again replaces it.'}
          </Alert>
        )}
        {shown && (
          <>
            <FormField label="You agree to">
              <Box variant="p" data-testid="standing-consent-text">
                {shown.NoticeText}
              </Box>
            </FormField>
            <Checkbox checked={agreed} onChange={({ detail }) => setAgreed(detail.checked)}>
              I have read this text and agree to it
            </Checkbox>
            <Box color="text-body-secondary" fontSize="body-s">
              The record keeps your email address, your account, the time and this text. It counts in a meeting only
              when you are on its participant list. You can still object during any meeting, and withdraw this consent
              at any time on this page.
            </Box>
            <Button variant="primary" onClick={give} loading={giving} disabled={!agreed}>
              Give consent
            </Button>
          </>
        )}
      </SpaceBetween>
    </Container>
  );
};

StandingConsentForm.propTypes = {
  answer: PropTypes.shape({
    Enabled: PropTypes.bool,
    MaxDays: PropTypes.number,
    Consents: PropTypes.arrayOf(PropTypes.shape({ Organizer: PropTypes.string })),
  }),
  onGiven: PropTypes.func.isRequired,
};

/**
 * The banner a Participant lands on after signing in, while they have no
 * consent on record and have not dismissed it. "Not now" only dismisses it.
 */
export const ConsentPromptBanner = ({ answer = null, sub = '' }) => {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed || !shouldPrompt(answer, sub)) return null;
  return (
    <Alert
      type="info"
      action={
        <Button
          onClick={() => {
            dismissPrompt(sub);
            setDismissed(true);
          }}
        >
          Not now
        </Button>
      }
    >
      {CONSENT_PROMPT_TEXT}
    </Alert>
  );
};

ConsentPromptBanner.propTypes = {
  answer: PropTypes.shape({ Enabled: PropTypes.bool, Consents: PropTypes.arrayOf(PropTypes.shape({})) }),
  sub: PropTypes.string,
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
              'You have not given consent in advance. Give a standing consent above, or open the consent link a ' +
                'meeting’s organizer sends you.'}
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
            From now on it is no longer in force: later meetings ask you in the meeting, and a meeting in progress that
            counted it treats this as your objection. The record of what you agreed to, and when you withdrew, is kept.
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
 * their role, with a standing consent they can give without a link; opened
 * from a meeting's consent link, that meeting's consent page instead.
 * `offerPrompt` (a Participant's pages) shows the banner a person with no
 * consent on record sees until they dismiss it.
 */
const MyConsentsPage = ({ offerPrompt = false }) => {
  const sub = sessionSub(useAppContext());
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
        {offerPrompt && <ConsentPromptBanner answer={answer} sub={sub} />}
        {meetingRef ? (
          <MeetingConsentInvitation meetingRef={meetingRef} onGiven={loadConsents} />
        ) : (
          <StandingConsentForm answer={answer} onGiven={loadConsents} />
        )}
        <MyConsentsList answer={answer} error={error} onWithdrawn={loadConsents} />
      </SpaceBetween>
    </ContentLayout>
  );
};

MyConsentsPage.propTypes = { offerPrompt: PropTypes.bool };

export default MyConsentsPage;
