/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedVpLoader - A chrome-free, programmable Virtual Participant starter.
 *
 * Modeled on EmbedMeetingLoader but for VPs: the parent page sends an
 * `LMA_CREATE_VP` postMessage with meeting details, this component creates
 * the VP (via the createVirtualParticipant GraphQL mutation + the
 * LMAVirtualParticipantSchedulerStateMachine Step Function), and emits
 * events back to the parent so it can drive dependent iframes
 * (vnc / transcript / summary / vp-details) — all of which will then
 * auto-refresh via AppSync once the VP joins and creates the call.
 *
 * Query params (for URL-driven flows):
 *   meetingName     - Pre-fill meeting name
 *   meetingPlatform - Pre-fill platform: ZOOM | TEAMS | CHIME | WEBEX
 *   meetingId       - Pre-fill meeting id
 *   meetingPassword - Pre-fill meeting password
 *   autoStart       - When true, auto-create the VP from URL params on load
 *
 * postMessage in (parent -> iframe):
 *   { type: 'LMA_CREATE_VP',
 *     meetingName, meetingPlatform, meetingId, meetingPassword }
 *   { type: 'LMA_END_VP', vpId }       -- end a VP
 *
 * postMessage out (iframe -> parent):
 *   { type: 'LMA_VP_LOADER_READY' }
 *   { type: 'LMA_VP_CREATED',
 *     vpId, status, meetingName, meetingPlatform, meetingId }
 *   { type: 'LMA_VP_STATUS_CHANGED',
 *     vpId, status, callId }           -- on every status update
 *   { type: 'LMA_VP_ERROR', error }
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import { fetchAuthSession, fetchUserAttributes } from 'aws-amplify/auth';
import React, { useState, useEffect, useCallback, useRef } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Select,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import { SFNClient, StartSyncExecutionCommand } from '@aws-sdk/client-sfn';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import awsExports from '../../aws-exports';

const client = generateClient();
const logger = new ConsoleLogger('EmbedVpLoader');

const STATES = {
  IDLE: 'idle', // Form visible, waiting for user action
  WAITING: 'waiting', // Waiting for LMA_CREATE_VP postMessage from parent
  CREATING: 'creating', // VP creation in flight
  CREATED: 'created', // VP record created & Step Function kicked off
  ERROR: 'error', // Something went wrong
};

const createVirtualParticipant = /* GraphQL */ `
  mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
    createVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      createdAt
      CallId
    }
  }
`;

const endVirtualParticipant = /* GraphQL */ `
  mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
    endVirtualParticipant(input: $input) {
      id
      status
      updatedAt
    }
  }
`;

const onUpdateVirtualParticipant = /* GraphQL */ `
  subscription OnUpdateVirtualParticipant {
    onUpdateVirtualParticipant {
      id
      status
      updatedAt
      CallId
    }
  }
`;

const PLATFORM_OPTIONS = [
  { label: 'Zoom', value: 'ZOOM' },
  { label: 'Microsoft Teams', value: 'TEAMS' },
  { label: 'Amazon Chime', value: 'CHIME' },
  { label: 'Cisco Webex', value: 'WEBEX' },
];

const EmbedVpLoader = ({ params, sendToParent }) => {
  const { user, currentCredentials } = useAppContext();
  const { settings } = useSettingsContext();

  const initialName = params.meetingName || '';
  const initialPlatform = params.meetingPlatform || 'ZOOM';
  const initialId = params.meetingId || '';
  const initialPassword = params.meetingPassword || '';

  const [meetingName, setMeetingName] = useState(initialName);
  const [meetingPlatform, setMeetingPlatform] = useState(initialPlatform);
  const [meetingId, setMeetingId] = useState(initialId);
  const [meetingPassword, setMeetingPassword] = useState(initialPassword);

  const [state, setState] = useState(
    // eslint-disable-next-line no-nested-ternary
    params.autoStart ? STATES.CREATING : initialName && initialId ? STATES.IDLE : STATES.WAITING,
  );
  const [errorMessage, setErrorMessage] = useState('');
  const [createdVp, setCreatedVp] = useState(null);

  // Track the vpId across async callbacks & subscriptions without causing
  // re-subscribes.
  const createdVpIdRef = useRef(null);

  const doCreate = useCallback(
    async (overrides = {}) => {
      setState(STATES.CREATING);
      setErrorMessage('');

      const effectiveName = overrides.meetingName ?? meetingName;
      const effectivePlatform = overrides.meetingPlatform ?? meetingPlatform;
      const effectiveId = overrides.meetingId ?? meetingId;
      const effectivePassword = overrides.meetingPassword ?? meetingPassword;

      if (!effectiveName || !effectiveId) {
        setState(STATES.ERROR);
        setErrorMessage('meetingName and meetingId are required');
        sendToParent({ type: 'LMA_VP_ERROR', error: 'meetingName and meetingId are required' });
        return;
      }

      try {
        // Resolve the current user identifier (email preferred)
        let email;
        try {
          const attrs = await fetchUserAttributes();
          email = attrs?.email;
        } catch (err) {
          logger.debug('fetchUserAttributes failed, falling back', err);
        }
        const userName =
          email ||
          user?.attributes?.email ||
          user?.signInDetails?.loginId ||
          user?.username ||
          'embed-user@example.com';

        // 1. Create the VP record via GraphQL
        const vpInput = {
          meetingName: effectiveName,
          meetingPlatform: effectivePlatform,
          meetingId: String(effectiveId).replace(/ /g, ''),
          meetingPassword: effectivePassword || '',
          status: 'INITIALIZING',
        };

        const vpResult = await client.graphql({
          query: createVirtualParticipant,
          variables: { input: vpInput },
        });

        const vp = vpResult?.data?.createVirtualParticipant;
        if (!vp?.id) {
          throw new Error('createVirtualParticipant did not return an id');
        }
        createdVpIdRef.current = vp.id;
        setCreatedVp(vp);

        // 2. Kick the Step Function for immediate execution, mirroring
        //    VirtualParticipantList.handleCreateParticipant.
        if (!settings.LMAVirtualParticipantSchedulerStateMachine) {
          throw new Error(
            'LMAVirtualParticipantSchedulerStateMachine is not configured in settings - VP service unavailable',
          );
        }

        const sfnClient = new SFNClient({
          region: awsExports.aws_project_region,
          credentials: currentCredentials,
        });

        const authSession = await fetchAuthSession();

        const sfnParams = {
          stateMachineArn: settings.LMAVirtualParticipantSchedulerStateMachine,
          input: JSON.stringify({
            apiInfo: { httpMethod: 'POST' },
            data: {
              meetingPlatform: effectivePlatform,
              meetingID: String(effectiveId).replace(/ /g, ''),
              meetingPassword: effectivePassword || '',
              meetingName: effectiveName,
              meetingTime: '',
              userName,
              virtualParticipantId: vp.id,
              accessToken: authSession?.tokens?.accessToken?.toString() || '',
              idToken: authSession?.tokens?.idToken?.toString() || '',
              rereshToken: '', // (sic: matches existing VP list implementation)
            },
          }),
        };

        const data = await sfnClient.send(new StartSyncExecutionCommand(sfnParams));

        if (data.status === 'FAILED') {
          let detail = 'Failed to start virtual participant';
          try {
            const parsed = data.output ? JSON.parse(data.output) : {};
            detail = parsed.errorMessage || parsed.error || detail;
          } catch (_err) {
            /* ignore */
          }
          throw new Error(detail);
        }

        setState(STATES.CREATED);

        sendToParent({
          type: 'LMA_VP_CREATED',
          vpId: vp.id,
          status: vp.status || 'INITIALIZING',
          meetingName: effectiveName,
          meetingPlatform: effectivePlatform,
          meetingId: String(effectiveId).replace(/ /g, ''),
          callId: vp.CallId || null,
        });
      } catch (err) {
        logger.error('Failed to create VP:', err);
        const msg = err?.message || 'Failed to create Virtual Participant';
        setState(STATES.ERROR);
        setErrorMessage(msg);
        sendToParent({ type: 'LMA_VP_ERROR', error: msg });
      }
    },
    [
      meetingName,
      meetingPlatform,
      meetingId,
      meetingPassword,
      currentCredentials,
      settings.LMAVirtualParticipantSchedulerStateMachine,
      user,
      sendToParent,
    ],
  );

  const doEnd = useCallback(
    async (vpIdToEnd) => {
      const id = vpIdToEnd || createdVpIdRef.current;
      if (!id) return;
      try {
        await client.graphql({
          query: endVirtualParticipant,
          variables: { input: { id } },
        });
      } catch (err) {
        logger.error('Failed to end VP:', err);
        sendToParent({ type: 'LMA_VP_ERROR', error: err?.message || 'Failed to end VP' });
      }
    },
    [sendToParent],
  );

  // Auto-start from URL params
  useEffect(() => {
    if (params.autoStart && initialName && initialId) {
      const t = setTimeout(() => doCreate(), 800);
      return () => clearTimeout(t);
    }
    return undefined;
    // eslint-disable-next-line
  }, [params.autoStart]);

  // Subscribe to VP status updates & forward the ones matching our vpId.
  useEffect(() => {
    const subscription = client.graphql({ query: onUpdateVirtualParticipant }).subscribe({
      next: (msg) => {
        const upd = msg?.data?.onUpdateVirtualParticipant;
        if (!upd?.id) return;
        if (upd.id !== createdVpIdRef.current) return;
        sendToParent({
          type: 'LMA_VP_STATUS_CHANGED',
          vpId: upd.id,
          status: upd.status,
          callId: upd.CallId || null,
        });
        setCreatedVp((prev) => ({ ...(prev || {}), ...upd }));
      },
      error: (err) => logger.error('VP status subscription error:', err),
    });
    return () => {
      try {
        subscription.unsubscribe();
      } catch (_err) {
        /* ignore */
      }
    };
  }, [sendToParent]);

  // Listen for parent control messages
  useEffect(() => {
    const handleMessage = (event) => {
      const { data } = event;
      if (!data?.type) return;
      switch (data.type) {
        case 'LMA_CREATE_VP':
          doCreate({
            meetingName: data.meetingName,
            meetingPlatform: data.meetingPlatform,
            meetingId: data.meetingId,
            meetingPassword: data.meetingPassword,
          });
          break;
        case 'LMA_END_VP':
          doEnd(data.vpId);
          break;
        case 'LMA_SET_VP_PARAMS':
          if (data.meetingName !== undefined) setMeetingName(data.meetingName);
          if (data.meetingPlatform !== undefined) setMeetingPlatform(data.meetingPlatform);
          if (data.meetingId !== undefined) setMeetingId(data.meetingId);
          if (data.meetingPassword !== undefined) setMeetingPassword(data.meetingPassword);
          sendToParent({ type: 'LMA_VP_PARAMS_SET' });
          break;
        default:
          break;
      }
    };
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [doCreate, doEnd, sendToParent]);

  // Notify parent we're ready
  useEffect(() => {
    sendToParent({
      type: 'LMA_VP_LOADER_READY',
      state,
      meetingName,
      meetingPlatform,
      meetingId,
    });
    // eslint-disable-next-line
  }, []);

  // ------------- Render -------------
  if (state === STATES.WAITING) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box margin={{ top: 'm' }} fontSize="heading-m">
            Ready to create a Virtual Participant
          </Box>
          <Box margin={{ top: 's' }} color="text-body-secondary">
            Waiting for meeting parameters...
          </Box>
          <Box margin={{ top: 's' }} color="text-body-secondary" fontSize="body-s">
            Send a postMessage of type <code>LMA_CREATE_VP</code> with
            <code> meetingName</code>, <code>meetingPlatform</code>, <code>meetingId</code>,
            <code> meetingPassword</code>.
          </Box>
        </Box>
      </Container>
    );
  }

  if (state === STATES.CREATING) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box margin={{ top: 'm' }} fontSize="heading-m">
            Creating Virtual Participant...
          </Box>
          <Box margin={{ top: 's' }} color="text-body-secondary">
            Starting the VP Step Function — this may take a moment.
          </Box>
        </Box>
      </Container>
    );
  }

  if (state === STATES.CREATED && createdVp) {
    return (
      <Container
        header={
          <Header
            variant="h3"
            actions={
              <Button variant="primary" onClick={() => doEnd(createdVp.id)}>
                End VP
              </Button>
            }
          >
            Virtual Participant Created
          </Header>
        }
      >
        <ColumnLayout columns={2} variant="text-grid">
          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              VP ID
            </Box>
            <div>
              <code>{createdVp.id}</code>
            </div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Status
            </Box>
            <div>{createdVp.status}</div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Meeting
            </Box>
            <div>{createdVp.meetingName}</div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Platform / ID
            </Box>
            <div>
              {createdVp.meetingPlatform} / {createdVp.meetingId}
            </div>
          </SpaceBetween>
        </ColumnLayout>
        <Box margin={{ top: 'l' }} color="text-body-secondary" fontSize="body-s">
          The parent page has been notified via <code>LMA_VP_CREATED</code>. Status updates will be sent via{' '}
          <code>LMA_VP_STATUS_CHANGED</code> including the <code>callId</code> when it becomes available.
        </Box>
      </Container>
    );
  }

  if (state === STATES.ERROR) {
    return (
      <Box padding="l">
        <Alert type="error" header="Virtual Participant Error">
          {errorMessage}
        </Alert>
        <Box margin={{ top: 'm' }} textAlign="center">
          <Button onClick={() => setState(STATES.IDLE)}>Try Again</Button>
        </Box>
      </Box>
    );
  }

  // IDLE — minimal form for users who want to drive it manually
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        doCreate();
      }}
    >
      <Container
        header={<Header variant="h3">Start a Virtual Participant</Header>}
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => doCreate()}>
              Create &amp; Join
            </Button>
          </Box>
        }
      >
        <ColumnLayout columns={2}>
          <FormField label="Meeting name">
            <Input value={meetingName} onChange={(e) => setMeetingName(e.detail.value)} placeholder="Weekly sync" />
          </FormField>
          <FormField label="Platform">
            <Select
              selectedOption={PLATFORM_OPTIONS.find((o) => o.value === meetingPlatform) || PLATFORM_OPTIONS[0]}
              onChange={(e) => setMeetingPlatform(e.detail.selectedOption.value)}
              options={PLATFORM_OPTIONS}
            />
          </FormField>
          <FormField label="Meeting ID">
            <Input value={meetingId} onChange={(e) => setMeetingId(e.detail.value)} placeholder="1234567890" />
          </FormField>
          <FormField label="Meeting password">
            <Input
              type="password"
              value={meetingPassword}
              onChange={(e) => setMeetingPassword(e.detail.value)}
              placeholder="Optional"
            />
          </FormField>
        </ColumnLayout>
      </Container>
    </form>
  );
};

EmbedVpLoader.propTypes = {
  params: PropTypes.shape({
    meetingName: PropTypes.string,
    meetingPlatform: PropTypes.string,
    meetingId: PropTypes.string,
    meetingPassword: PropTypes.string,
    autoStart: PropTypes.bool,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default EmbedVpLoader;
