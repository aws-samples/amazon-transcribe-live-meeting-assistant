/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState, useRef, useCallback, useEffect } from 'react';

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
  Tiles,
  FileUpload,
  Checkbox,
  ProgressBar,
  Alert,
} from '@cloudscape-design/components';
import '@cloudscape-design/global-styles/index.css';
import useWebSocket from 'react-use-websocket';
import { generateClient } from 'aws-amplify/api';

import { DEFAULT_OTHER_SPEAKER_NAME, DEFAULT_LOCAL_SPEAKER_NAME, SYSTEM } from '../common/constants';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { getTimestampStr } from '../common/utilities';
import createUploadMeeting from '../../graphql/queries/createUploadMeeting';

let SOURCE_SAMPLING_RATE;
const DEFAULT_BLANK_FIELD_MSG = 'This will be set back to the default value if left blank.';

// Mode values for the top-of-page Tiles selector.
const MODE_STREAM = 'stream';
const MODE_UPLOAD = 'upload';

// Upload-phase labels shown in the progress card.
const UPLOAD_PHASE = {
  IDLE: 'idle',
  REQUESTING_URL: 'requesting_url',
  UPLOADING: 'uploading',
  DONE: 'done',
  ERROR: 'error',
};

// AppSync client — reused only for the createUploadMeeting mutation.
const appsyncClient = generateClient();

const StreamAudio = () => {
  const { currentSession, user } = useAppContext();
  const { settings } = useSettingsContext();
  // Amplify v6 exposes tokens as currentSession.tokens.{accessToken,idToken}.toString().
  // The refresh token is not exposed via fetchAuthSession in v6; pass an empty string
  // since the websocket server's JWT verifier only strictly requires access/id tokens.
  const JWT_TOKEN = currentSession?.tokens?.accessToken?.toString() ?? '';
  const ID_TOKEN = currentSession?.tokens?.idToken?.toString() ?? '';
  const REFRESH_TOKEN = '';

  const userIdentifier = user?.attributes?.email || user?.signInDetails?.loginId || DEFAULT_LOCAL_SPEAKER_NAME;

  // --- Shared meeting-metadata form state (used by both modes) ------------
  const [mode, setMode] = useState(MODE_STREAM);
  const [meetingTopic, setMeetingTopic] = useState('Stream Audio');
  const [callMetaData, setCallMetaData] = useState({
    callId: `${meetingTopic} - ${getTimestampStr()}`,
    agentId: userIdentifier,
    fromNumber: DEFAULT_OTHER_SPEAKER_NAME,
    toNumber: SYSTEM,
  });

  // --- Streaming mode state (unchanged behavior) --------------------------
  const [recording, setRecording] = useState(false);
  const [streamingStarted, setStreamingStarted] = useState(false);
  const [isFlashing, setIsFlashing] = useState(false);
  const [micMuted, setMicMuted] = useState(false);
  const [recordedMeetingId, setRecordedMeetingId] = useState('');

  // --- Upload mode state --------------------------------------------------
  const [uploadFiles, setUploadFiles] = useState([]);
  const [enableDiarization, setEnableDiarization] = useState(false);
  const [maxSpeakers, setMaxSpeakers] = useState('4');
  const [uploadPhase, setUploadPhase] = useState(UPLOAD_PHASE.IDLE);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadError, setUploadError] = useState(null);
  const [uploadedCallId, setUploadedCallId] = useState('');
  const uploadXhrRef = useRef(null);

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
    console.log(`DEBUG - [${new Date().toISOString()}]: Trying to resolve websocket url...`);
    return new Promise((resolve) => {
      if (settings.WSEndpoint) {
        console.log(`
          DEBUG - [${new Date().toISOString()}]: Resolved Websocket URL to ${settings.WSEndpoint}
        `);
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
    onOpen: (event) => {
      console.log(`
        DEBUG - [${new Date().toISOString()}]: Websocket onOpen Event: ${JSON.stringify(event)}
      `);
    },
    onClose: (event) => {
      console.log(`
        DEBUG - [${new Date().toISOString()}]: Websocket onClose Event: ${JSON.stringify(event)}
      `);
    },
    onError: (event) => {
      console.log(`
        DEBUG - [${new Date().toISOString()}]: Websocket onError Event: ${JSON.stringify(event)}
      `);
    },
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
    setCallMetaData({
      ...callMetaData,
      agentId: e.detail.value,
    });
  };

  const handlefromNumberChange = (e) => {
    setCallMetaData({
      ...callMetaData,
      fromNumber: e.detail.value,
    });
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
    console.log(`DEBUG - [${new Date().toISOString()}]: Stopping recording...`);
    if (audioProcessor.current) {
      audioProcessor.current.port.postMessage({
        message: 'UPDATE_RECORDING_STATE',
        setRecording: false,
      });
      audioProcessor.current.port.close();
      audioProcessor.current.disconnect();
      setMicMuted(false);
      setRecordedMeetingId(callMetaData.callId);
    } else {
      console.log(`
        DEBUG - [${new Date().toISOString()}]: Error trying to stop recording. AudioWorklet Processor node is not active.
      `);
    }
    if (streamingStarted && !recording) {
      callMetaData.callEvent = 'END';
      // eslint-disable-next-line prettier/prettier
      console.log(`
        DEBUG - [${new Date().toISOString()}]: Send Call END msg: ${JSON.stringify(callMetaData)}
      `);
      sendMessage(JSON.stringify(callMetaData));
      setStreamingStarted(false);
      setCallMetaData({
        ...callMetaData,
        callId: crypto.randomUUID(),
      });
    }
    setRecording(false);
  };

  // Default any missing fields in the call metadata
  // The callMetaData state is updated so onscreen fields are updated, but a copy is returned
  //  to avoid the scenario of the state not updating before it is used
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
    console.log(`
      DEBUG - [${new Date().toISOString()}]: Start Recording and Streaming Audio to Websocket server.
    `);
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

      // eslint-disable-next-line prettier/prettier
      console.log(`DEBUG - [${new Date().toISOString()}]: Send Call START msg: ${JSON.stringify(recordingCallMetaData)}`);
      sendMessage(JSON.stringify(recordingCallMetaData));
      setStreamingStarted(true);

      displayAudioSource.current = audioContext.current.createMediaStreamSource(displayStream.current);
      micAudioSource.current = audioContext.current.createMediaStreamSource(micStream.current);

      const monoDisplaySource = convertToMono(displayAudioSource.current);
      const monoMicSource = convertToMono(micAudioSource.current);

      channelMerger.current = audioContext.current.createChannelMerger(2);
      monoMicSource.connect(channelMerger.current, 0, 0);
      monoDisplaySource.connect(channelMerger.current, 0, 1);

      console.log(`
        DEBUG - [${new Date().toISOString()}]: Registering and adding AudioWorklet processor to capture audio
      `);
      try {
        await audioContext.current.audioWorklet.addModule('./worklets/recording-processor.js');
      } catch (error) {
        console.log(`
          DEBUG - [${new Date().toISOString()}]: Error registering AudioWorklet processor: ${error}
        `);
      }

      audioProcessor.current = new AudioWorkletNode(audioContext.current, 'recording-processor');

      audioProcessor.current.port.onmessageerror = (error) => {
        console.log(`
          DEBUG - [${new Date().toISOString()}]: Error receiving message from worklet ${error}
        `);
      };

      audioProcessor.current.port.onmessage = (event) => {
        // this is pcm audio
        sendMessage(event.data);
      };
      channelMerger.current.connect(audioProcessor.current);
    } catch (error) {
      alert(`An error occurred while recording: ${error}`);
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
      // eslint-disable-next-line no-restricted-globals
      agreeToRecord.current = confirm(settings.recordingDisclaimer);

      if (agreeToRecord.current) {
        if (settings.WSEndpoint) {
          setRecording(!recording);
        } else {
          alert('Enable Websocket Audio input to use this feature');
        }
      }
    } else {
      setRecording(!recording);
    }
    return recording;
  };

  const toggleMicrophoneEnabled = () => {
    micStream.current.getAudioTracks()[0].enabled = !micStream.current.getAudioTracks()[0].enabled;
    setMicMuted(!micStream.current.getAudioTracks()[0].enabled);
  };

  // ------------------------------------------------------------------------
  // Upload-mode helpers
  // ------------------------------------------------------------------------

  /**
   * Put the file to S3 via a presigned URL, reporting progress.
   * Returns a Promise that resolves on HTTP 2xx or rejects with an Error.
   * Uses XMLHttpRequest so we can surface progress events (fetch() cannot).
   */
  const putFileToS3 = (file, presignedUrl, contentType) =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      uploadXhrRef.current = xhr;
      xhr.open('PUT', presignedUrl, true);
      xhr.setRequestHeader('Content-Type', contentType);
      xhr.upload.onprogress = (evt) => {
        if (evt.lengthComputable) {
          const pct = Math.round((evt.loaded / evt.total) * 100);
          setUploadProgress(pct);
        }
      };
      xhr.onload = () => {
        uploadXhrRef.current = null;
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve();
        } else {
          reject(new Error(`S3 upload failed: HTTP ${xhr.status} ${xhr.statusText}`));
        }
      };
      xhr.onerror = () => {
        uploadXhrRef.current = null;
        reject(new Error('Network error while uploading to S3.'));
      };
      xhr.onabort = () => {
        uploadXhrRef.current = null;
        reject(new Error('Upload cancelled.'));
      };
      xhr.send(file);
    });

  const resetUploadForm = () => {
    setUploadFiles([]);
    setUploadProgress(0);
    setUploadError(null);
    setUploadedCallId('');
    setUploadPhase(UPLOAD_PHASE.IDLE);
  };

  const cancelUpload = () => {
    if (uploadXhrRef.current) {
      uploadXhrRef.current.abort();
    }
  };

  const uploadDisabled =
    uploadFiles.length !== 1 ||
    !meetingTopic ||
    !callMetaData.agentId ||
    !callMetaData.fromNumber ||
    uploadPhase === UPLOAD_PHASE.REQUESTING_URL ||
    uploadPhase === UPLOAD_PHASE.UPLOADING;

  const handleUploadSubmit = async () => {
    setUploadError(null);
    setUploadProgress(0);
    setUploadedCallId('');

    const file = uploadFiles[0];
    if (!file) {
      setUploadError('Please select an audio or video file.');
      return;
    }
    if (!/^(audio|video)\//.test(file.type || '')) {
      setUploadError(
        `Selected file has unsupported content type "${file.type || 'unknown'}". Please pick an audio/* or video/* file.`,
      );
      return;
    }

    try {
      // 1. Ask AppSync for a presigned PUT URL + callId.
      setUploadPhase(UPLOAD_PHASE.REQUESTING_URL);
      // eslint-disable-next-line no-useless-escape
      const meetingPrefix = (meetingTopic || 'Uploaded Meeting').replace(/[\/?#%\+&]/g, '|');
      const input = {
        meetingTopic: meetingPrefix,
        agentId: callMetaData.agentId || DEFAULT_LOCAL_SPEAKER_NAME,
        fromNumber: callMetaData.fromNumber || DEFAULT_OTHER_SPEAKER_NAME,
        toNumber: callMetaData.toNumber || SYSTEM,
        filename: file.name,
        contentType: file.type || 'application/octet-stream',
        fileSize: file.size,
        enableDiarization,
        maxSpeakers: Number.parseInt(maxSpeakers, 10) || 4,
      };
      const response = await appsyncClient.graphql({
        query: createUploadMeeting,
        variables: { input },
      });

      const result = response?.data?.createUploadMeeting;
      if (!result?.uploadUrl || !result?.callId) {
        throw new Error('Server did not return a presigned upload URL.');
      }

      // 2. PUT the file to S3 directly from the browser.
      setUploadPhase(UPLOAD_PHASE.UPLOADING);
      await putFileToS3(file, result.uploadUrl, result.contentType || input.contentType);

      // 3. Hand off to the backend pipeline (Stage 2 will take it from here).
      setUploadedCallId(result.callId);
      setUploadProgress(100);
      setUploadPhase(UPLOAD_PHASE.DONE);
    } catch (err) {
      console.error('Upload failed:', err);
      setUploadError(err.message || String(err));
      setUploadPhase(UPLOAD_PHASE.ERROR);
    }
  };

  const submitLabel = (() => {
    if (mode === MODE_STREAM) return recording ? 'Stop Streaming' : 'Start Streaming';
    switch (uploadPhase) {
      case UPLOAD_PHASE.REQUESTING_URL:
        return 'Preparing upload…';
      case UPLOAD_PHASE.UPLOADING:
        return `Uploading… ${uploadProgress}%`;
      case UPLOAD_PHASE.DONE:
        return 'Upload new file';
      default:
        return 'Upload & Transcribe';
    }
  })();

  const handleSubmit = async () => {
    if (mode === MODE_STREAM) {
      handleRecording();
      return;
    }
    if (uploadPhase === UPLOAD_PHASE.DONE || uploadPhase === UPLOAD_PHASE.ERROR) {
      resetUploadForm();
      return;
    }
    await handleUploadSubmit();
  };

  const uploadInProgress = uploadPhase === UPLOAD_PHASE.UPLOADING || uploadPhase === UPLOAD_PHASE.REQUESTING_URL;

  return (
    <div>
      <form onSubmit={(e) => e.preventDefault()}>
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {mode === MODE_UPLOAD && uploadInProgress && (
                <Button variant="link" onClick={cancelUpload}>
                  Cancel
                </Button>
              )}
              <Button
                variant={
                  // eslint-disable-next-line no-nested-ternary
                  mode === MODE_STREAM ? (recording ? 'secondary' : 'primary') : 'primary'
                }
                onClick={handleSubmit}
                disabled={mode === MODE_UPLOAD ? uploadDisabled : false}
              >
                {submitLabel}
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween direction="vertical" size="l">
            <Container
              header={
                <Header variant="h2" description="Choose how you want to add a meeting to LMA.">
                  Input mode
                </Header>
              }
            >
              <Tiles
                value={mode}
                onChange={({ detail }) => setMode(detail.value)}
                items={[
                  {
                    value: MODE_STREAM,
                    label: 'Stream live from a browser tab',
                    description:
                      'Capture a meeting happening right now in another Chrome tab (the existing Stream Audio behavior).',
                  },
                  {
                    value: MODE_UPLOAD,
                    label: 'Upload a pre-recorded audio or video file',
                    description:
                      'Send an existing recording to LMA. We will transcribe it with Amazon Transcribe and generate the meeting summary automatically.',
                  },
                ]}
              />
            </Container>

            <Container
              header={
                <Header
                  variant="h2"
                  actions={
                    <div>
                      {mode === MODE_STREAM && recording && (
                        <Button href={`#/calls/${callMetaData.callId}`} variant="link" iconName="external" target="blank">
                          Open in progress meeting
                        </Button>
                      )}
                      {mode === MODE_UPLOAD && uploadedCallId && (
                        <Button
                          href={`#/calls/${uploadedCallId}`}
                          variant="link"
                          iconName="external"
                          target="blank"
                        >
                          Open meeting detail
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
                  <Input value={meetingTopic} onChange={handleCallIdChange} disabled={recording || uploadInProgress} />
                </FormField>
                <FormField
                  label={mode === MODE_UPLOAD ? 'Participants' : 'Participants (stream)'}
                  stretch
                  required
                  description={
                    mode === MODE_UPLOAD
                      ? 'Label for the remote side of the conversation'
                      : 'Label for stream audio'
                  }
                  errorText={callMetaData.fromNumber.length < 1 && DEFAULT_BLANK_FIELD_MSG}
                >
                  <Input
                    value={callMetaData.fromNumber}
                    onChange={handlefromNumberChange}
                    disabled={recording || uploadInProgress}
                  />
                </FormField>

                <FormField
                  label={mode === MODE_UPLOAD ? 'Meeting owner' : 'Meeting owner (microphone)'}
                  stretch
                  required
                  description={mode === MODE_UPLOAD ? 'Label for the meeting owner' : 'Label for microphone input'}
                  errorText={callMetaData.agentId.length < 1 && DEFAULT_BLANK_FIELD_MSG}
                >
                  <Grid gridDefinition={[{ colspan: 10 }, { colspan: 1 }]}>
                    <Input
                      value={callMetaData.agentId}
                      onChange={handleAgentIdChange}
                      disabled={recording || uploadInProgress}
                    />
                    {mode === MODE_STREAM && (
                      <Button
                        variant={micMuted ? 'secondary' : 'primary'}
                        onClick={toggleMicrophoneEnabled}
                        disabled={!recording}
                        iconAlign="left"
                        iconName={micMuted ? 'microphone-off' : 'microphone'}
                      />
                    )}
                  </Grid>
                </FormField>
              </ColumnLayout>

              {recording && mode === MODE_STREAM && (
                <Box
                  margin={{ top: 'xl' }}
                  float="right"
                  color={isFlashing && recording ? 'text-status-error' : 'text-body-secondary'}
                >
                  Recording in progress, do not close or refresh this tab.
                </Box>
              )}
            </Container>

            {mode === MODE_UPLOAD && (
              <Container
                header={
                  <Header
                    variant="h2"
                    description="Pick a recording and (optionally) enable speaker diarization for Amazon Transcribe's batch job."
                  >
                    Recording
                  </Header>
                }
              >
                <SpaceBetween direction="vertical" size="m">
                  <FormField
                    label="Audio or video file"
                    description="Accepts audio/* and video/* formats supported by Amazon Transcribe (wav, mp3, mp4, m4a, webm, flac, ogg, amr)."
                    errorText={uploadError || undefined}
                  >
                    <FileUpload
                      value={uploadFiles}
                      onChange={({ detail }) => {
                        setUploadFiles(detail.value);
                        setUploadError(null);
                      }}
                      showFileLastModified
                      showFileSize
                      accept="audio/*,video/*"
                      i18nStrings={{
                        uploadButtonText: (e) => (e ? 'Choose files' : 'Choose file'),
                        dropzoneText: (e) => (e ? 'Drop files to upload' : 'Drop file to upload'),
                        removeFileAriaLabel: (e) => `Remove file ${e + 1}`,
                        limitShowFewer: 'Show fewer files',
                        limitShowMore: 'Show more files',
                        errorIconAriaLabel: 'Error',
                      }}
                      constraintText="Max file size 5 GB. Uploaded directly to S3 from your browser — the file never transits a Lambda or API Gateway."
                    />
                  </FormField>

                  <ColumnLayout columns={2}>
                    <FormField
                      label="Enable speaker diarization"
                      description="Identifies up to N distinct speakers from a mixed audio track. Adds a few seconds to the Transcribe job."
                    >
                      <Checkbox
                        checked={enableDiarization}
                        onChange={({ detail }) => setEnableDiarization(detail.checked)}
                        disabled={uploadInProgress}
                      >
                        Diarize speakers
                      </Checkbox>
                    </FormField>
                    <FormField
                      label="Max speakers"
                      description="2–30. Used when diarization is enabled."
                      errorText={
                        enableDiarization &&
                        (Number.isNaN(Number.parseInt(maxSpeakers, 10)) ||
                          Number.parseInt(maxSpeakers, 10) < 2 ||
                          Number.parseInt(maxSpeakers, 10) > 30)
                          ? 'Must be between 2 and 30'
                          : undefined
                      }
                    >
                      <Input
                        value={maxSpeakers}
                        type="number"
                        onChange={({ detail }) => setMaxSpeakers(detail.value)}
                        disabled={!enableDiarization || uploadInProgress}
                      />
                    </FormField>
                  </ColumnLayout>

                  {(uploadPhase === UPLOAD_PHASE.UPLOADING ||
                    uploadPhase === UPLOAD_PHASE.REQUESTING_URL ||
                    uploadPhase === UPLOAD_PHASE.DONE) && (
                    <ProgressBar
                      status={uploadPhase === UPLOAD_PHASE.DONE ? 'success' : 'in-progress'}
                      value={uploadProgress}
                      label={
                        // eslint-disable-next-line no-nested-ternary
                        uploadPhase === UPLOAD_PHASE.REQUESTING_URL
                          ? 'Preparing secure upload…'
                          : uploadPhase === UPLOAD_PHASE.DONE
                          ? 'Upload complete. Transcription will begin shortly.'
                          : 'Uploading file to Amazon S3…'
                      }
                      description={uploadFiles[0]?.name}
                    />
                  )}

                  {uploadPhase === UPLOAD_PHASE.ERROR && uploadError && (
                    <Alert type="error" header="Upload failed">
                      {uploadError}
                    </Alert>
                  )}

                  {uploadPhase === UPLOAD_PHASE.DONE && uploadedCallId && (
                    <Alert type="success" header="Upload complete">
                      <SpaceBetween direction="vertical" size="xs">
                        <span>
                          The recording has been uploaded and a new meeting has been queued for transcription.
                        </span>
                        <span>
                          Meeting ID: <code>{uploadedCallId}</code>
                        </span>
                        <Link href={`#/calls/${uploadedCallId}`} external>
                          Open meeting detail page
                        </Link>
                      </SpaceBetween>
                    </Alert>
                  )}
                </SpaceBetween>
              </Container>
            )}
          </SpaceBetween>
        </Form>
      </form>
      {mode === MODE_STREAM && !recording && recordedMeetingId !== '' && (
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

export default StreamAudio;
