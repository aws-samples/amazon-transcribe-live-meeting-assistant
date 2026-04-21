/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedMeetingLoader - A blank loading/starter page for meetings.
 * Can pre-populate meeting fields, auto-start, or wait for parent control.
 *
 * This component provides a clean meeting initiation interface that can:
 * 1. Show a pre-populated form and wait for user to click Start
 * 2. Auto-start a meeting with provided parameters
 * 3. Wait for a postMessage from the parent to start
 * 4. Show a loading state while a meeting is being set up
 *
 * Query params:
 *   meetingTopic  - Pre-fill meeting topic
 *   participants  - Pre-fill participant label
 *   owner         - Pre-fill meeting owner
 *   autoStart     - Auto-start streaming (true/false)
 *   callId        - If provided, redirect to call details view
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useState, useEffect, useCallback, useRef } from 'react';
import PropTypes from 'prop-types';
import {
  Box,
  Button,
  Container,
  Header,
  SpaceBetween,
  Spinner,
  FormField,
  Input,
  ColumnLayout,
  Alert,
} from '@cloudscape-design/components';
import '@cloudscape-design/global-styles/index.css';
import useWebSocket from 'react-use-websocket';
import { DEFAULT_OTHER_SPEAKER_NAME, DEFAULT_LOCAL_SPEAKER_NAME, SYSTEM } from '../common/constants';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { getTimestampStr } from '../common/utilities';

const logger = new ConsoleLogger('EmbedMeetingLoader');

let SOURCE_SAMPLING_RATE;

/**
 * States for the meeting loader:
 * - idle: Showing form, waiting for user action
 * - waiting: Waiting for parent postMessage to start
 * - starting: Meeting is being started
 * - active: Meeting is in progress
 * - stopped: Meeting has ended
 * - error: An error occurred
 */
const STATES = {
  IDLE: 'idle',
  WAITING: 'waiting',
  STARTING: 'starting',
  ACTIVE: 'active',
  STOPPED: 'stopped',
  ERROR: 'error',
};

const EmbedMeetingLoader = ({ params, sendToParent }) => {
  const { currentSession, user } = useAppContext();
  const { settings } = useSettingsContext();
  // Amplify v6 exposes tokens via currentSession.tokens.{accessToken,idToken}.toString()
  const JWT_TOKEN = currentSession?.tokens?.accessToken?.toString() ?? '';
  const ID_TOKEN = currentSession?.tokens?.idToken?.toString() ?? '';
  const REFRESH_TOKEN = '';

  const userIdentifier = user?.attributes?.email || user?.signInDetails?.loginId || DEFAULT_LOCAL_SPEAKER_NAME;

  const initialTopic = params.meetingTopic || '';
  const initialParticipants = params.participants || '';
  const initialOwner = params.owner || userIdentifier;

  const [meetingTopic, setMeetingTopic] = useState(initialTopic);
  const [participants, setParticipants] = useState(initialParticipants);
  const [owner, setOwner] = useState(initialOwner);
  // eslint-disable-next-line no-nested-ternary
  const [state, setState] = useState(params.autoStart ? STATES.STARTING : initialTopic ? STATES.IDLE : STATES.WAITING);
  const [errorMessage, setErrorMessage] = useState('');
  const [callId, setCallId] = useState(params.callId || '');
  const [isFlashing, setIsFlashing] = useState(false);

  const audioProcessor = useRef();
  const audioContext = useRef();
  const displayStream = useRef();
  const micStream = useRef();
  const displayAudioSource = useRef();
  const micAudioSource = useRef();
  const channelMerger = useRef();

  // Flashing indicator for active recording
  useEffect(() => {
    let interval;
    if (state === STATES.ACTIVE) {
      interval = setInterval(() => setIsFlashing((prev) => !prev), 500);
    } else {
      setIsFlashing(false);
    }
    return () => clearInterval(interval);
  }, [state]);

  const getSocketUrl = useCallback(() => {
    return new Promise((resolve) => {
      if (settings.WSEndpoint) {
        resolve(settings.WSEndpoint);
      }
    });
  }, [settings.WSEndpoint]);

  const { sendMessage } = useWebSocket(getSocketUrl, {
    queryParams: {
      authorization: `Bearer ${JWT_TOKEN}`,
      id_token: ID_TOKEN,
      refresh_token: REFRESH_TOKEN,
    },
    onOpen: () => logger.debug('WebSocket connected'),
    onClose: () => logger.debug('WebSocket closed'),
    onError: (event) => logger.error('WebSocket error:', event),
    shouldReconnect: () => true,
  });

  const convertToMono = (audioSource) => {
    const splitter = audioContext.current.createChannelSplitter(2);
    const merger = audioContext.current.createChannelMerger(1);
    audioSource.connect(splitter);
    splitter.connect(merger, 0, 0);
    splitter.connect(merger, 1, 0);
    return merger;
  };

  const stopMeeting = async () => {
    logger.debug('Stopping meeting...');
    if (audioProcessor.current) {
      audioProcessor.current.port.postMessage({
        message: 'UPDATE_RECORDING_STATE',
        setRecording: false,
      });
      audioProcessor.current.port.close();
      audioProcessor.current.disconnect();
    }

    if (callId) {
      const endMeta = {
        callId,
        agentId: owner || DEFAULT_LOCAL_SPEAKER_NAME,
        fromNumber: participants || DEFAULT_OTHER_SPEAKER_NAME,
        toNumber: SYSTEM,
        callEvent: 'END',
      };
      sendMessage(JSON.stringify(endMeta));
    }

    setState(STATES.STOPPED);
    sendToParent({ type: 'LMA_MEETING_STOPPED', callId });
  };

  const startMeeting = async () => {
    logger.debug('Starting meeting...');
    setState(STATES.STARTING);

    // eslint-disable-next-line no-useless-escape
    const topic = (meetingTopic || 'Meeting').replace(/[\/?#%\+&]/g, '|');
    const newCallId = `${topic} - ${getTimestampStr()}`;

    try {
      if (!settings.WSEndpoint) {
        throw new Error('WebSocket endpoint not configured');
      }

      audioContext.current = new window.AudioContext();
      displayStream.current = await window.navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: true,
        selfBrowserSurface: 'exclude',
      });

      micStream.current = await window.navigator.mediaDevices.getUserMedia({
        video: false,
        audio: true,
      });

      SOURCE_SAMPLING_RATE = audioContext.current.sampleRate;

      const callMetaData = {
        callId: newCallId,
        agentId: owner || DEFAULT_LOCAL_SPEAKER_NAME,
        fromNumber: participants || DEFAULT_OTHER_SPEAKER_NAME,
        toNumber: SYSTEM,
        samplingRate: SOURCE_SAMPLING_RATE,
        callEvent: 'START',
      };

      sendMessage(JSON.stringify(callMetaData));
      setCallId(newCallId);

      displayAudioSource.current = audioContext.current.createMediaStreamSource(displayStream.current);
      micAudioSource.current = audioContext.current.createMediaStreamSource(micStream.current);

      const monoDisplaySource = convertToMono(displayAudioSource.current);
      const monoMicSource = convertToMono(micAudioSource.current);

      channelMerger.current = audioContext.current.createChannelMerger(2);
      monoMicSource.connect(channelMerger.current, 0, 0);
      monoDisplaySource.connect(channelMerger.current, 0, 1);

      try {
        await audioContext.current.audioWorklet.addModule('./worklets/recording-processor.js');
      } catch (error) {
        logger.error('Error registering AudioWorklet processor:', error);
      }

      audioProcessor.current = new AudioWorkletNode(audioContext.current, 'recording-processor');
      audioProcessor.current.port.onmessage = (event) => sendMessage(event.data);
      channelMerger.current.connect(audioProcessor.current);

      setState(STATES.ACTIVE);
      sendToParent({ type: 'LMA_MEETING_STARTED', callId: newCallId });
    } catch (error) {
      logger.error('Failed to start meeting:', error);
      setState(STATES.ERROR);
      setErrorMessage(error.message || 'Failed to start meeting');
      sendToParent({
        type: 'LMA_MEETING_ERROR',
        error: error.message || 'Failed to start meeting',
      });
    }
  };

  // Auto-start support
  useEffect(() => {
    if (params.autoStart && settings.WSEndpoint && state === STATES.STARTING) {
      const timer = setTimeout(() => startMeeting(), 1500);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [params.autoStart, settings.WSEndpoint]);

  // Listen for parent control messages
  useEffect(() => {
    const handleMessage = (event) => {
      const { data } = event;
      if (!data || !data.type) return;

      switch (data.type) {
        case 'LMA_START_MEETING':
          // Parent can also pass meeting params
          if (data.meetingTopic) setMeetingTopic(data.meetingTopic);
          if (data.participants) setParticipants(data.participants);
          if (data.owner) setOwner(data.owner);
          startMeeting();
          break;
        case 'LMA_STOP_MEETING':
          stopMeeting();
          break;
        case 'LMA_SET_MEETING_PARAMS':
          if (data.meetingTopic) setMeetingTopic(data.meetingTopic);
          if (data.participants) setParticipants(data.participants);
          if (data.owner) setOwner(data.owner);
          sendToParent({ type: 'LMA_PARAMS_SET' });
          break;
        default:
          break;
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [state]);

  // Notify parent we're ready
  useEffect(() => {
    sendToParent({
      type: 'LMA_MEETING_LOADER_READY',
      state,
      meetingTopic,
      participants,
      owner,
    });
  }, []);

  // Render based on state
  if (state === STATES.WAITING) {
    return (
      <div className="embed-meeting-loader">
        <Container>
          <Box textAlign="center" padding="xxl">
            <Spinner size="large" />
            <Box margin={{ top: 'm' }} fontSize="heading-m">
              Ready to start a meeting
            </Box>
            <Box margin={{ top: 's' }} color="text-body-secondary">
              Waiting for meeting parameters...
            </Box>
            <Box margin={{ top: 's' }} color="text-body-secondary" fontSize="body-s">
              Send a postMessage with type &apos;LMA_START_MEETING&apos; to begin, or &apos;LMA_SET_MEETING_PARAMS&apos;
              to configure.
            </Box>
          </Box>
        </Container>
      </div>
    );
  }

  if (state === STATES.STARTING) {
    return (
      <div className="embed-meeting-loader">
        <Container>
          <Box textAlign="center" padding="xxl">
            <Spinner size="large" />
            <Box margin={{ top: 'm' }} fontSize="heading-m">
              Starting meeting...
            </Box>
            <Box margin={{ top: 's' }} color="text-body-secondary">
              Setting up audio capture and WebSocket connection.
            </Box>
          </Box>
        </Container>
      </div>
    );
  }

  if (state === STATES.ACTIVE) {
    return (
      <div className="embed-meeting-loader">
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button href={`#/calls/${callId}`} variant="link" iconName="external" target="blank">
                    Open meeting details
                  </Button>
                  <Button variant="primary" onClick={stopMeeting}>
                    Stop Meeting
                  </Button>
                </SpaceBetween>
              }
            >
              Meeting In Progress
            </Header>
          }
        >
          <ColumnLayout columns={3} variant="text-grid">
            <SpaceBetween size="xs">
              <Box color="text-label" fontWeight="bold">
                Topic
              </Box>
              <div>{meetingTopic || 'Meeting'}</div>
            </SpaceBetween>
            <SpaceBetween size="xs">
              <Box color="text-label" fontWeight="bold">
                Participants
              </Box>
              <div>{participants || DEFAULT_OTHER_SPEAKER_NAME}</div>
            </SpaceBetween>
            <SpaceBetween size="xs">
              <Box color="text-label" fontWeight="bold">
                Owner
              </Box>
              <div>{owner}</div>
            </SpaceBetween>
          </ColumnLayout>
          <Box
            margin={{ top: 'l' }}
            textAlign="center"
            color={isFlashing ? 'text-status-error' : 'text-body-secondary'}
          >
            🔴 Recording in progress — do not close this tab
          </Box>
        </Container>
      </div>
    );
  }

  if (state === STATES.STOPPED) {
    return (
      <div className="embed-meeting-loader">
        <Container>
          <Box textAlign="center" padding="xxl">
            <Box fontSize="heading-m">Meeting Ended</Box>
            <Box margin={{ top: 's' }} color="text-body-secondary">
              The meeting has been stopped.
            </Box>
            {callId && (
              <Box margin={{ top: 'm' }}>
                <Button href={`#/calls/${callId}`} variant="primary" iconName="external" target="blank">
                  View Meeting Recording
                </Button>
              </Box>
            )}
            <Box margin={{ top: 'm' }}>
              <Button onClick={() => setState(STATES.IDLE)}>Start New Meeting</Button>
            </Box>
          </Box>
        </Container>
      </div>
    );
  }

  if (state === STATES.ERROR) {
    return (
      <div className="embed-meeting-loader">
        <Box padding="l">
          <Alert type="error" header="Meeting Error">
            {errorMessage}
          </Alert>
          <Box margin={{ top: 'm' }} textAlign="center">
            <Button onClick={() => setState(STATES.IDLE)}>Try Again</Button>
          </Box>
        </Box>
      </div>
    );
  }

  // IDLE state - show form
  return (
    <div className="embed-meeting-loader">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          startMeeting();
        }}
      >
        <Container
          header={<Header variant="h2">Start a Meeting</Header>}
          footer={
            <Box float="right">
              <Button variant="primary" onClick={startMeeting}>
                Start Meeting
              </Button>
            </Box>
          }
        >
          <ColumnLayout columns={2}>
            <FormField label="Meeting Topic" description="Name for this meeting">
              <Input
                value={meetingTopic}
                onChange={(e) => setMeetingTopic(e.detail.value)}
                placeholder="Enter meeting topic..."
              />
            </FormField>
            <FormField label="Participants" description="Label for stream audio participants">
              <Input
                value={participants}
                onChange={(e) => setParticipants(e.detail.value)}
                placeholder="Other Participant"
              />
            </FormField>
            <FormField label="Meeting Owner" description="Your identifier">
              <Input value={owner} onChange={(e) => setOwner(e.detail.value)} placeholder={userIdentifier} />
            </FormField>
          </ColumnLayout>
        </Container>
      </form>
    </div>
  );
};

EmbedMeetingLoader.propTypes = {
  params: PropTypes.shape({
    meetingTopic: PropTypes.string,
    participants: PropTypes.string,
    owner: PropTypes.string,
    autoStart: PropTypes.bool,
    callId: PropTypes.string,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default EmbedMeetingLoader;
