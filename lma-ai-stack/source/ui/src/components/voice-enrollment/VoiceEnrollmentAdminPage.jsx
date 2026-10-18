/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Invite people to enrol their voice.
 *
 * An admin can ask; only the person asked can agree. That asymmetry is the
 * whole design, so this page is deliberately narrow: it creates the ability for
 * somebody to decide for themselves and records who asked them. There is no
 * control here that enrols anybody, and there cannot be one -- the backend
 * refuses a consent granted by anyone other than its subject.
 *
 * Addresses are taken one per line, because the realistic job is a room full of
 * people off a meeting invite rather than one person at a time. Each is sent
 * separately and reported separately: one address that cannot be invited must
 * not cost the others their invitation.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  ContentLayout,
  FormField,
  Header,
  SpaceBetween,
  StatusIndicator,
  Textarea,
  TextContent,
} from '@cloudscape-design/components';

import { inviteVoiceEnrollee } from '../../graphql/queries/voiceEnrollment';
import { VOICE_ENROLLMENT_PATH } from '../../routes/constants';

const client = generateClient();
const logger = new ConsoleLogger('VoiceEnrollmentAdminPage');

/**
 * A cheap plausibility check, so an obvious typo is caught before a round trip.
 * The backend validates properly, including the deployment's allowed sign-up
 * domain, and is the authority on whether an address can be invited.
 */
const LOOKS_LIKE_AN_EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/**
 * Split pasted text into addresses.
 *
 * Newlines, commas and semicolons all separate, because an address list copied
 * out of a mail client or a calendar invite uses whichever it happens to use.
 * Angle-bracket display names ("Sarah Lee <sarah@example.com>") are reduced to
 * the address, since that is what somebody pasting a recipient line will have.
 */
export const parseInviteList = (text) => {
  const seen = new Set();
  const addresses = [];
  (text || '')
    .split(/[\n,;]+/)
    .map((entry) => {
      const bracketed = entry.match(/<([^>]+)>/);
      return (bracketed ? bracketed[1] : entry).trim();
    })
    .filter(Boolean)
    .forEach((address) => {
      const key = address.toLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        addresses.push(address);
      }
    });
  return addresses;
};

const VoiceEnrollmentAdminPage = () => {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [results, setResults] = useState([]);

  const addresses = parseInviteList(text);
  const invalid = addresses.filter((address) => !LOOKS_LIKE_AN_EMAIL.test(address));

  const send = useCallback(async () => {
    setSending(true);
    setResults([]);
    const outcomes = [];
    // Sequential rather than concurrent: this creates Cognito users, and a
    // burst of admin API calls is the one thing that would turn a long paste
    // into a throttled, half-finished one.
    for (let index = 0; index < addresses.length; index += 1) {
      const address = addresses[index];
      if (!LOOKS_LIKE_AN_EMAIL.test(address)) {
        outcomes.push({ address, ok: false, message: 'that does not look like an email address' });
      } else {
        try {
          // eslint-disable-next-line no-await-in-loop
          const response = await client.graphql({
            query: inviteVoiceEnrollee,
            variables: { input: { Email: address } },
          });
          const result = response?.data?.inviteVoiceEnrollee;
          outcomes.push({
            address,
            ok: Boolean(result?.Success),
            message: result?.Message || (result?.Success ? 'invitation sent' : 'not sent'),
          });
        } catch (error) {
          logger.error('Invitation failed', error);
          outcomes.push({ address, ok: false, message: 'could not send that invitation' });
        }
      }
      setResults([...outcomes]);
    }
    setSending(false);
    if (outcomes.every((outcome) => outcome.ok)) {
      setText('');
    }
  }, [addresses]);

  const portalUrl = `${window.location.origin}${window.location.pathname}#${VOICE_ENROLLMENT_PATH}`;
  const sent = results.filter((result) => result.ok).length;

  return (
    <ContentLayout header={<Header variant="h1">Voice Enrollment</Header>}>
      <SpaceBetween size="l">
        <Container header={<Header variant="h2">What an invitation does</Header>}>
          <TextContent>
            <p>
              An invitation gives somebody an account that can reach the voice-enrollment portal and nothing else — no
              meetings and no configuration. It asks them to consider enrolling; it is not a consent, and it does not
              create a voiceprint.
            </p>
            <p>
              Only the person invited can agree, after reading the terms in their own session. Declining is a normal
              outcome and leaves nothing stored. They can withdraw at any time from the same page, without asking you,
              which deletes their voiceprint immediately.
            </p>
            <p>
              The portal is at <code>{portalUrl}</code>. Somebody who already has an LMA account can go there directly;
              an invitation is for people who do not.
            </p>
          </TextContent>
        </Container>

        <Container header={<Header variant="h2">Invite people to enrol</Header>}>
          <SpaceBetween size="m">
            <FormField
              label="Email addresses"
              description="One per line, or separated by commas. Pasting a recipient line from a meeting invite works."
              errorText={invalid.length > 0 ? `Not an email address: ${invalid.slice(0, 3).join(', ')}` : undefined}
            >
              <Textarea
                value={text}
                onChange={({ detail }) => setText(detail.value)}
                placeholder={'sarah@example.com\npriya@example.com'}
                rows={5}
                disabled={sending}
              />
            </FormField>
            <Box>
              <Button
                variant="primary"
                onClick={send}
                disabled={sending || addresses.length === 0 || invalid.length > 0}
                loading={sending}
              >
                {addresses.length > 1 ? `Send ${addresses.length} invitations` : 'Send invitation'}
              </Button>
            </Box>
            {results.length > 0 && (
              <SpaceBetween size="xs">
                <Box variant="awsui-key-label">
                  {sent} of {results.length} sent
                </Box>
                {results.map((result) => (
                  <StatusIndicator key={result.address} type={result.ok ? 'success' : 'error'}>
                    {result.address} — {result.message}
                  </StatusIndicator>
                ))}
              </SpaceBetween>
            )}
          </SpaceBetween>
        </Container>

        <Alert type="info" header="Who has enrolled is not listed here">
          An enrollment is the enrollee&apos;s own record, and this page does not read it. To see or change your own,
          use the portal. A person who has left can be removed by withdrawing on their behalf, which is an audited
          action — see the voice enrollment documentation.
        </Alert>
      </SpaceBetween>
    </ContentLayout>
  );
};

export default VoiceEnrollmentAdminPage;
