/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedStreamAudio - Embeddable Stream Audio component without navigation chrome.
 * Supports pre-populating fields via query params and auto-starting.
 *
 * Query params:
 *   meetingTopic  - Pre-fill meeting topic
 *   participants  - Pre-fill participant label (stream audio)
 *   owner         - Pre-fill meeting owner (microphone label)
 *   autoStart     - Auto-start streaming on load (true/false)
 */
import React, { useState, useRef, useCallback, useEffect } from 'react';
import PropTypes from 'prop-types';
import {
  Form,
  FormField,
  SpaceBetween,
  Container,
  Button,
  Input,
  Header,
  ColumnLayout,
  Grid,
  Box,
  Link,
} from '@awsui/components-react';
import '@awsui/global-styles/index.css';
import useWebSocket from 'react-use-websocket';
import { Logger } from 'aws-amplify';

import { DEFAULT_OTHER_SPEAKER_NAME, DEFAULT_LOCAL_SPEAKER_NAME, SYSTEM } from '../common/constants';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { getTimestampStr } from '../common/utilities';

const logger = new Logger('EmbedStreamAudio');

let SOURCE_SAMPLING_RATE;
const DEFAULT_BLANK_FIELD_MSG = 'This will be set back to the default value if left blank.';

const EmbedStreamAudio = ({ params, sendToParent }) => {
  const { currentSession, user } = useAppContext();
  const { settings } = useSettingsContext();
  const JWT_TOKEN = currentSession.getAccessToken().getJwtToken();

  const userIdentifier = user?.attributes?.email || DEFAULT_LOCAL_SPEAKER_NAME;

  // Use query params for initial values, falling back to defaults
  const initialTopic = params.meetingTopic || 'Stream Audio';
  const initialParticipants = params.participants || DEFAULT_OTHER_SPEAKER_NAME;
  const initialOwner = params.owner || userIdentifier;

  const [meetingTopic, setMeetingTopic] = useState(initialTopic);
  const [callMetaData, setCallMetaData] = useState({
    callId: `${initialTopic} - ${getTimestampStr()}`,
    agentId: initialOwner,
    fromNumber: initialParticipants,
    toNumber: SYSTEM,
  });

  const [recording, setRecording] = useState(false);
  const [streamingStarted, setStreamingStarted] = useState(false);
  const [isFlashing, setIsFlashing] = useState(false);
  const [micMuted, setMicMuted] = useState(false);
  const [recordedMeetingId, setRecordedMeetingId] = useState('');
  const [autoStartTriggered, setAutoStartTriggered] = useState(false);

  useEffect(() => {
    let interval;
    if (recording) {
      interval = setInterval(() => {
        setIsFlashing((prevState) => !prevState);
      }, 500);
    } else {
      clearInterval(interval);
      setIsFlashing(false);
    }
    return () => clearInterval(interval);
  }, [recording]);

  const getSocketUrl = useCallback(() => {
    logger.debug('Trying to resolve websocket url...');
    return new Promise((resolve) => {
      if (settings.WSEndpoint) {
        logger.debug(`Resolved Websocket URL to ${settings.WSEndpoint}`);
        resolve(settings.WSEndpoint);
      }
    });
  }, [settings.WSEndpoint]);

  const { sendMessage } = useWebSocket(getSocketUrl, {
    queryParams: {
      authorization: `Bearer ${JWT_TOKEN}`,
      id_token: `${currentSession.idToken.jwtToken}`,
      refresh_token: `${currentSession.refreshToken.token}`,
    },
    onOpen: () => logger.debug('Websocket connected'),
    onClose: () => logger.debug('Websocket closed'),
    onError: (event) => logger.error('Websocket error:', event),
    shouldReconnect: () => true,
  });

  const handleCallIdChange = (e) => {
    setMeetingTopic(e.detail.value);
    setCallMetaData({
      ...callMetaData,
      callId: `${e.detail.value} - ${getTimestampStr()}`,
    });
  };

  const handleAgentIdChange = (e) => {
    setCallMetaData({ ...callMetaData, agentId: e.detail.value });
  };

  const handlefromNumberChange = (e) => {
    setCallMetaData({ ...callMetaData, fromNumber: e.detail.value });
  };

  const audioProcessor = useRef();
  const audioContext = useRef();
  const displayStream = useRef();
  const micStream = useRef();
  const displayAudioSource = useRef();
  const micAudioSource = useRef();
  const channelMerger = useRef();
  const agreeToRecord = useRef();

  const convertToMono = (audioSource) => {
    const splitter = audioContext.current.createChannelSplitter(2);
    const merger = audioContext.current.createChannelMerger(1);
    audioSource.connect(splitter);
    splitter.connect(merger, 0, 0);
    splitter.connect(merger, 1, 0);
    return merger;
  };

  const stopRecording = async () => {
    logger.debug('Stopping recording...');
    if (audioProcessor.current) {
      audioProcessor.current.port.postMessage({
        message: 'UPDATE_RECORDING_STATE',
        setRecording: false,
      });
      audioProcessor.current.port.close();
      audioProcessor.current.disconnect();
      setMicMuted(false);
      setRecordedMeetingId(callMetaData.callId);
    }
    if (streamingStarted && !recording) {
      callMetaData.callEvent = 'END';
      sendMessage(JSON.stringify(callMetaData));
      setStreamingStarted(false);

      // Notify parent
      sendToParent({
        type: 'LMA_MEETING_STOPPED',
        callId: callMetaData.callId,
      });

      setCallMetaData({
        ...callMetaData,
        callId: crypto.randomUUID(),
      });
    }
    setRecording(false);
  };

  const getFinalCallMetadata = () => {
    // eslint-disable-next-line no-useless-escape
    const meetingPrefix = meetingTopic.replace(/[\/?#%\+&]/g, '|') || 'Stream Audio';
    setMeetingTopic(meetingPrefix);
    const callMetaDataCopy = {
      ...callMetaData,
      callId: `${meetingPrefix} - ${getTimestampStr()}`,
      agentId: callMetaData.agentId || DEFAULT_LOCAL_SPEAKER_NAME,
      fromNumber: callMetaData.fromNumber || DEFAULT_OTHER_SPEAKER_NAME,
    };
    setCallMetaData(callMetaDataCopy);
    return callMetaDataCopy;
  };

  const startRecording = async () => {
    logger.debug('Start Recording and Streaming Audio');
    const recordingCallMetaData = getFinalCallMetadata();
    try {
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

      recordingCallMetaData.samplingRate = SOURCE_SAMPLING_RATE;
      recordingCallMetaData.callEvent = 'START';

      sendMessage(JSON.stringify(recordingCallMetaData));
      setStreamingStarted(true);

      // Notify parent
      sendToParent({
        type: 'LMA_MEETING_STARTED',
        callId: recordingCallMetaData.callId,
      });

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

      audioProcessor.current.port.onmessageerror = (error) => {
        logger.error('Error receiving message from worklet:', error);
      };

      audioProcessor.current.port.onmessage = (event) => {
        sendMessage(event.data);
      };
      channelMerger.current.connect(audioProcessor.current);
    } catch (error) {
      sendToParent({
        type: 'LMA_MEETING_ERROR',
        error: error.message || 'Failed to start recording',
      });
      await stopRecording();
    }
  };

  async function toggleRecording() {
    if (recording) {
      await startRecording();
    } else {
      await stopRecording();
    }
  }

  useEffect(() => {
    toggleRecording();
  }, [recording]);

  const handleRecording = () => {
    if (!recording) {
      // For autoStart, skip the disclaimer
      if (params.autoStart && !autoStartTriggered) {
        agreeToRecord.current = true;
      } else {
        // eslint-disable-next-line no-restricted-globals
        agreeToRecord.current = confirm(settings.recordingDisclaimer);
      }

      if (agreeToRecord.current) {
        if (settings.WSEndpoint) {
          setRecording(!recording);
        } else {
          sendToParent({
            type: 'LMA_MEETING_ERROR',
            error: 'WebSocket endpoint not configured',
          });
        }
      }
    } else {
      setRecording(!recording);
    }
    return recording;
  };

  // Auto-start support
  useEffect(() => {
    if (params.autoStart && !autoStartTriggered && settings.WSEndpoint) {
      setAutoStartTriggered(true);
      // Small delay to ensure WebSocket is connected
      const timer = setTimeout(() => {
        handleRecording();
      }, 1500);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [params.autoStart, settings.WSEndpoint, autoStartTriggered]);

  // Listen for parent control messages
  useEffect(() => {
    const handleMessage = (event) => {
      const { data } = event;
      if (!data || !data.type) return;

      if (data.type === 'LMA_START_MEETING' && !recording) {
        handleRecording();
      } else if (data.type === 'LMA_STOP_MEETING' && recording) {
        setRecording(false);
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [recording]);

  const toggleMicrophoneEnabled = () => {
    micStream.current.getAudioTracks()[0].enabled = !micStream.current.getAudioTracks()[0].enabled;
    setMicMuted(!micStream.current.getAudioTracks()[0].enabled);
  };

  return (
    <div className="embed-stream-audio">
      <form onSubmit={(e) => e.preventDefault()}>
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant={recording ? 'secondary' : 'primary'} onClick={handleRecording} disabled={false}>
                {recording ? 'Stop Streaming' : 'Start Streaming'}
              </Button>
            </SpaceBetween>
          }
        >
          <Container
            header={
              <Header
                variant="h2"
                actions={
                  <div>
                    {recording && (
                      <Button href={`#/calls/${callMetaData.callId}`} variant="link" iconName="external" target="blank">
                        Open in progress meeting
                      </Button>
                    )}
                  </div>
                }
              >
                Meeting Information
              </Header>
            }
          >
            <ColumnLayout columns={2}>
              <FormField
                label="Meeting Topic"
                stretch
                required
                description="Prefix for unique meeting identifier"
                errorText={meetingTopic.length < 1 && DEFAULT_BLANK_FIELD_MSG}
              >
                <Input value={meetingTopic} onChange={handleCallIdChange} disabled={recording} />
              </FormField>
              <FormField
                label="Participants (stream)"
                stretch
                required
                description="Label for stream audio"
                errorText={callMetaData.fromNumber.length < 1 && DEFAULT_BLANK_FIELD_MSG}
              >
                <Input value={callMetaData.fromNumber} onChange={handlefromNumberChange} disabled={recording} />
              </FormField>

              <FormField
                label="Meeting owner (microphone)"
                stretch
                required
                description="Label for microphone input"
                errorText={callMetaData.agentId.length < 1 && DEFAULT_BLANK_FIELD_MSG}
              >
                <Grid gridDefinition={[{ colspan: 10 }, { colspan: 1 }]}>
                  <Input value={callMetaData.agentId} onChange={handleAgentIdChange} disabled={recording} />
                  <Button
                    variant={micMuted ? 'secondary' : 'primary'}
                    onClick={toggleMicrophoneEnabled}
                    disabled={!recording}
                    iconAlign="left"
                    iconName={micMuted ? 'microphone-off' : 'microphone'}
                  />
                </Grid>
              </FormField>
            </ColumnLayout>

            {recording && (
              <Box
                margin={{ top: 'xl' }}
                float="right"
                color={isFlashing && recording ? 'text-status-error' : 'text-body-secondary'}
              >
                Recording in progress, do not close or refresh this tab.
              </Box>
            )}
          </Container>
        </Form>
      </form>
      {!recording && recordedMeetingId !== '' && (
        <Box margin={{ top: 'xl' }} float="right" color="text-label">
          <SpaceBetween direction="horizontal" size="s">
            <span>Stream ended:</span>
            <Link href={`#/calls/${recordedMeetingId}`} external>
              Open recorded meeting
            </Link>
          </SpaceBetween>
        </Box>
      )}
    </div>
  );
};

EmbedStreamAudio.propTypes = {
  params: PropTypes.shape({
    meetingTopic: PropTypes.string,
    participants: PropTypes.string,
    owner: PropTypes.string,
    autoStart: PropTypes.bool,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default EmbedStreamAudio;
