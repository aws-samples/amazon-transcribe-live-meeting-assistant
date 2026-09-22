/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable @typescript-eslint/no-empty-function */
import React, { createContext, startTransition, useCallback, useContext, useEffect, useRef, useState } from 'react';
import useWebSocket, { ReadyState } from 'react-use-websocket';
import { useSettings } from './SettingsContext';
import { useUserContext } from './UserContext';
import { WebSocketHook } from 'react-use-websocket/dist/lib/types';
import { applyMuteAndPause, formatTimestamp, platformFromBaseUrl, UNKNOWN_PLATFORM } from '../lib/capture';

type Call = {
  callEvent: string,
  agentId: string,
  fromNumber: string,
  toNumber: string,
  callId: string,
  samplingRate: number,
  activeSpeaker: string,
}

const initialIntegration = {
  currentCall: {} as Call,
  isTranscribing: false,
  muted: false,
  setMuted: (muteValue: boolean) => { },
  paused: false,
  setPaused: (pauseValue: boolean) => { },
  fetchMetadata: () => { },
  startTranscription: (user: any, userName: string, meetingTopic: string) => { },
  stopTranscription: () => { },
  metadata: {
    userName: "",
    meetingTopic: ""
  },
  platform: "n/a",
  activeSpeaker: "n/a",
  sendRecordingMessage: () => { }
};
const IntegrationContext = createContext(initialIntegration);

function IntegrationProvider({ children }: any) {

  const [currentCall, setCurrentCall] = useState({} as Call);
  const { user, checkTokenExpired, login } = useUserContext();
  const settings = useSettings();
  const [metadata, setMetadata] = useState({
    userName: "",
    meetingTopic: ""
  });
  const [platform, setPlatform] = useState("n/a");
  const [activeSpeaker, setActiveSpeaker] = useState("n/a");
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [shouldConnect, setShouldConnect] = useState(false);
  const [muted, setMuted] = useState(false);
  const [paused, setPaused] = useState(false);

  const { sendMessage, readyState, getWebSocket } = useWebSocket(settings.wssEndpoint as string, {
    queryParams: {
      authorization: `Bearer ${user.access_token}`,
      id_token: `${user.id_token}`,
      refresh_token: `${user.refresh_token}`
    },
    onOpen: (event) => {
      console.log(event);
    },
    onClose: (event) => {
      console.log(event);
      stopTranscription();
    },
    onError: (event) => {
      console.log(event);
      stopTranscription();
    },
  }, shouldConnect);

  const connectionStatus = {
    [ReadyState.CONNECTING]: 'Connecting',
    [ReadyState.OPEN]: 'Open',
    [ReadyState.CLOSING]: 'Closing',
    [ReadyState.CLOSED]: 'Closed',
    [ReadyState.UNINSTANTIATED]: 'Uninstantiated',
  }[readyState];

  const dataUrlToBytes = async (dataUrl: string, isMuted: boolean, isPaused: boolean) => {
    const res = await fetch(dataUrl);
    const dataArray = new Uint8Array(await res.arrayBuffer());
    return applyMuteAndPause(dataArray, isMuted, isPaused);
  }

  const updateMetadata = useCallback((newMetadata: any) => {
    const detected = platformFromBaseUrl(newMetadata && newMetadata.baseUrl);
    // Left as-is when unrecognised, so a tab change away from a meeting does not
    // clear a platform that is still being captured.
    if (detected !== UNKNOWN_PLATFORM) {
      setPlatform(detected);
    }

    setMetadata(newMetadata);
  }, [metadata, setMetadata, platform, setPlatform]);

  const fetchMetadata = async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { action: "FetchMetadata" });
      console.log("Received response from Metadata query!", response);
      updateMetadata(response);
    }
    return {};
  }

  const sendRecordingMessage = useCallback(async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { action: "SendChatMessage", message: settings.recordingMessage });
    }
    return {};
  }, [settings]);

  const sendStopMessage = useCallback(async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { action: "SendChatMessage", message: settings.stopRecordingMessage });
    }
    return {};
  }, [settings]);

  const startTranscription = useCallback(async (user: any, userName: string, meetingTopic: string) => {
    if (await checkTokenExpired(user)) {
      login();
      return;
    }

    setShouldConnect(true);
    const callMetadata = {
      callEvent: 'START',
      agentId: userName,
      fromNumber: '+9165551234',
      toNumber: '+8001112222',
      callId: `${meetingTopic} - ${formatTimestamp(new Date())}`,
      samplingRate: 8000,
      activeSpeaker: 'n/a'
    }

    setCurrentCall(callMetadata);

    try {
      if (chrome.runtime) {
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (tab.id) {
          const response = await chrome.tabs.sendMessage(tab.id, { action: "StartTranscription" });
          // We send a message here, but not actually start the stream until we receive a new message with the sample rate.
        }
      }
    } catch (exception) {
      alert("If you recently installed or update LMA, please refresh the browser's page and try again.");
    }
  }, [setShouldConnect, setCurrentCall]);

  const stopTranscription = useCallback(() => {
    if (isTranscribing) {
      if (chrome.runtime) {
        chrome.runtime.sendMessage({ action: "StopTranscription" });
      }
      if (readyState === ReadyState.OPEN) {
        currentCall.callEvent = 'END';
        sendMessage(JSON.stringify(currentCall));
        getWebSocket()?.close();
      }
      setShouldConnect(false);
      setIsTranscribing(false);
      setPaused(false);
      sendStopMessage();
    }
  }, [readyState, shouldConnect, isTranscribing, paused, setIsTranscribing, getWebSocket, sendMessage, setPaused, sendStopMessage, sendRecordingMessage]);

  useEffect(() => {
    if (chrome.runtime) {
      const handleRuntimeMessage = async (request: any, sender: any, sendResponse: any) => {
        if (request.action === "TranscriptionStopped") {
          stopTranscription();
        } else if (request.action === "UpdateMetadata") {
          updateMetadata(request.metadata);
        } else if (request.action === "SamplingRate") {
          // This event should only bubble up once at the start of recording in the injected code
          currentCall.samplingRate = request.samplingRate;
          currentCall.callEvent = 'START';
          sendMessage(JSON.stringify(currentCall));
          setIsTranscribing(true);
          sendRecordingMessage();
        } else if (request.action === "AudioData") {
          if (readyState === ReadyState.OPEN) {
            const audioData = await dataUrlToBytes(request.audio, muted, paused);
            sendMessage(audioData);
          }
        } else if (request.action === "ActiveSpeakerChange") {
          currentCall.callEvent = 'SPEAKER_CHANGE';
          currentCall.activeSpeaker = request.active_speaker;
          setActiveSpeaker(request.active_speaker);
          sendMessage(JSON.stringify(currentCall));
        } else if (request.action === "MuteChange") {
          setMuted(request.mute);
        }
      };
      chrome.runtime.onMessage.addListener(handleRuntimeMessage);
      // Clean up the listener when the component unmounts
      return () => chrome.runtime.onMessage.removeListener(handleRuntimeMessage);
    }
  }, [currentCall, metadata, readyState, muted, paused, activeSpeaker, isTranscribing, setMuted,
    setActiveSpeaker, sendMessage, setPlatform, setIsTranscribing, sendRecordingMessage, updateMetadata
  ]);

  return (
    <IntegrationContext.Provider value={{
      currentCall, isTranscribing, muted, setMuted, paused, setPaused,
      fetchMetadata, startTranscription, stopTranscription, metadata, platform,
      activeSpeaker, sendRecordingMessage
    }}>
      {children}
    </IntegrationContext.Provider>
  );
}
export function useIntegration() {
  return useContext(IntegrationContext);
}
export default IntegrationProvider;