// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
  Select,
} from '@awsui/components-react';
import '@awsui/global-styles/index.css';
import useWebSocket from 'react-use-websocket';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';

// const TARGET_SAMPLING_RATE = 8000;
let SOURCE_SAMPLING_RATE;

// export const downsampleBuffer = (buffer, inputSampleRate = 44100, outputSampleRate = 16000) => {
//   if (outputSampleRate === inputSampleRate) {
//     return buffer;
//   }

//   const sampleRateRatio = inputSampleRate / outputSampleRate;
//   const newLength = Math.round(buffer.length / sampleRateRatio);
//   const result = new Float32Array(newLength);
//   let offsetResult = 0;
//   let offsetBuffer = 0;

//   while (offsetResult < result.length) {
//     const nextOffsetBuffer = Math.round((offsetResult + 1) * sampleRateRatio);
//     let accum = 0;
//     let count = 0;

//     for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i += 1) {
//       accum += buffer[i];
//       count += 1;
//     }
//     result[offsetResult] = accum / count;
//     offsetResult += 1;
//     offsetBuffer = nextOffsetBuffer;
//   }
//   return result;
// };

const StreamAudio = () => {
  const { currentSession } = useAppContext();
  const { settings } = useSettingsContext();
  const JWT_TOKEN = currentSession.getAccessToken().getJwtToken();

  const [callMetaData, setCallMetaData] = useState({
    callId: crypto.randomUUID(),
    agentId: 'AudioStream',
    fromNumber: '+9165551234',
    toNumber: '+8001112222',
  });

  const [recording, setRecording] = useState(false);
  const [streamingStarted, setStreamingStarted] = useState(false);
  const [micInputOption, setMicInputOption] = useState({ label: 'AGENT', value: 'agent' });

  const getSocketUrl = useCallback(() => {
    console.log('Trying to resolve websocket url...');
    return new Promise((resolve) => {
      if (settings.WSEndpoint) {
        console.log(`Resolved Websocket URL to ${settings.WSEndpoint}`);
        resolve(settings.WSEndpoint);
      }
    });
  }, [settings.WSEndpoint]);

  const { sendMessage } = useWebSocket(getSocketUrl, {
    queryParams: {
      authorization: `Bearer ${JWT_TOKEN}`,
    },
    onOpen: (event) => {
      console.log(event);
    },
    onClose: (event) => {
      console.log(event);
    },
    onError: (event) => {
      console.log(event);
    },
  });

  const handleCallIdChange = (e) => {
    setCallMetaData({
      ...callMetaData,
      callId: e.detail.value,
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

  const handletoNumberChange = (e) => {
    setCallMetaData({
      ...callMetaData,
      toNumber: e.detail.value,
    });
  };

  const handleMicInputOptionSelection = (e) => {
    setMicInputOption(e.detail.selectedOption);
  };

  const audioProcessor = useRef();
  const audioContext = useRef();
  const displayStream = useRef();
  const micStream = useRef();
  const displayAudioSource = useRef();
  const micAudioSource = useRef();
  const channelMerger = useRef();
  const destination = useRef();
  const audioData = useRef();

  const pcmEncode = (input) => {
    const buffer = new ArrayBuffer(input.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < input.length; i += 1) {
      const s = Math.max(-1, Math.min(1, input[i]));
      view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buffer;
  };

  const interleave = (lbuffer, rbuffer) => {
    // const leftAudioBuffer = pcmEncode(
    //   downsampleBuffer(lbuffer, SOURCE_SAMPLING_RATE, TARGET_SAMPLING_RATE),
    // );
    const leftAudioBuffer = pcmEncode(lbuffer);
    const leftView = new DataView(leftAudioBuffer);

    // const rightAudioBuffer = pcmEncode(
    //   downsampleBuffer(rbuffer, SOURCE_SAMPLING_RATE, TARGET_SAMPLING_RATE),
    // );
    const rightAudioBuffer = pcmEncode(rbuffer);
    const rightView = new DataView(rightAudioBuffer);

    const buffer = new ArrayBuffer(leftAudioBuffer.byteLength * 2);
    const view = new DataView(buffer);

    for (let i = 0, j = 0; i < leftAudioBuffer.byteLength; i += 2, j += 4) {
      view.setInt16(j, leftView.getInt16(i, true), true);
      view.setInt16(j + 2, rightView.getInt16(i, true), true);
    }
    return buffer;
  };

  const stopRecording = async () => {
    if (audioProcessor.current) {
      audioProcessor.current.port.postMessage({
        message: 'UPDATE_RECORDING_STATE',
        setRecording: false,
      });
      audioProcessor.current.port.close();
      audioProcessor.current.disconnect();
    } else {
      console.log('no media recorder available to stop');
    }
    if (streamingStarted && !recording) {
      callMetaData.callEvent = 'END';
      sendMessage(JSON.stringify(callMetaData));
      setStreamingStarted(false);
      setCallMetaData({
        ...callMetaData,
        callId: crypto.randomUUID(),
      });
    }
  };

  const startRecording = async () => {
    try {
      audioContext.current = new window.AudioContext();
      displayStream.current = await window.navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: {
          noiseSuppression: true,
          autoGainControl: true,
          echoCancellation: true,
        },
      });

      micStream.current = await window.navigator.mediaDevices.getUserMedia({
        video: false,
        audio: {
          noiseSuppression: true,
          autoGainControl: true,
          echoCancellation: true,
        },
      });
      SOURCE_SAMPLING_RATE = audioContext.current.sampleRate;

      // callMetaData.samplingRate = TARGET_SAMPLING_RATE;
      callMetaData.samplingRate = SOURCE_SAMPLING_RATE;

      callMetaData.callEvent = 'START';
      sendMessage(JSON.stringify(callMetaData));
      setStreamingStarted(true);

      displayAudioSource.current = audioContext.current.createMediaStreamSource(
        new MediaStream([displayStream.current.getAudioTracks()[0]]),
      );
      micAudioSource.current = audioContext.current.createMediaStreamSource(
        new MediaStream([micStream.current.getAudioTracks()[0]]),
      );

      channelMerger.current = audioContext.current.createChannelMerger(2);
      displayAudioSource.current.connect(channelMerger.current, 0, 0);
      micAudioSource.current.connect(channelMerger.current, 0, 1);

      try {
        await audioContext.current.audioWorklet.addModule('./worklets/recording-processor.js');
      } catch (error) {
        console.log(`Add module error ${error}`);
      }

      audioProcessor.current = new AudioWorkletNode(audioContext.current, 'recording-processor', {
        processorOptions: {
          numberOfChannels: 2,
          sampleRate: SOURCE_SAMPLING_RATE,
          maxFrameCount: (audioContext.current.sampleRate * 1) / 10,
        },
      });

      audioProcessor.current.port.postMessage({
        message: 'UPDATE_RECORDING_STATE',
        setRecording: true,
      });

      destination.current = audioContext.current.createMediaStreamDestination();
      channelMerger.current.connect(audioProcessor.current).connect(destination.current);

      audioProcessor.current.port.onmessageerror = (error) => {
        console.log(`Error receving message from worklet ${error}`);
      };

      // buffer[0] - display stream,  buffer[1] - mic stream
      audioProcessor.current.port.onmessage = (event) => {
        if (micInputOption.value === 'agent') {
          audioData.current = new Uint8Array(
            interleave(event.data.buffer[0], event.data.buffer[1]),
          );
        } else {
          audioData.current = new Uint8Array(
            interleave(event.data.buffer[1], event.data.buffer[0]),
          );
        }
        sendMessage(audioData.current);
      };
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
    if (settings.WSEndpoint) {
      setRecording(!recording);
    } else {
      alert('Enable Websocket Audio input to use this feature');
    }
    return recording;
  };

  return (
    <form onSubmit={(e) => e.preventDefault()}>
      <Form
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="primary" onClick={handleRecording}>
              {recording ? 'Stop Streaming' : 'Start Streaming'}
            </Button>
          </SpaceBetween>
        }
      >
        <Container header={<Header variant="h2">Call Meta data</Header>}>
          <ColumnLayout columns={2}>
            <FormField label="Call ID" stretch required description="Auto-generated Unique call ID">
              <Input value={callMetaData.callId} onChange={handleCallIdChange} />
            </FormField>
            <FormField label="Agent ID" stretch required description="Agent ID">
              <Input value={callMetaData.agentId} onChange={handleAgentIdChange} />
            </FormField>
            <FormField label="Customer Phone" stretch required description="Customer Phone">
              <Input value={callMetaData.fromNumber} onChange={handlefromNumberChange} />
            </FormField>
            <FormField label="System Phone" stretch required description="System Phone">
              <Input value={callMetaData.toNumber} onChange={handletoNumberChange} />
            </FormField>
            <FormField label="Microphone Role" stretch required description="Mic input">
              <Select
                selectedOption={micInputOption}
                onChange={handleMicInputOptionSelection}
                options={[
                  { label: 'CALLER', value: 'caller' },
                  { label: 'AGENT', value: 'agent' },
                ]}
              />
            </FormField>
          </ColumnLayout>
        </Container>
      </Form>
    </form>
  );
};

export default StreamAudio;
