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
import { applyMuteAndPause, formatTimestamp, MeetingMetadata, MetadataForTab, mergeMetadataForTab, platformFromBaseUrl, ReportSource, UNKNOWN_PLATFORM } from '../lib/capture';

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
  metadata: { userName: "", meetingTopic: "" } as MeetingMetadata,
  platform: "n/a",
  activeSpeaker: "n/a",
  sendRecordingMessage: () => { }
};
const IntegrationContext = createContext(initialIntegration);

function IntegrationProvider({ children }: any) {

  const [currentCall, setCurrentCall] = useState({} as Call);
  const { user, checkTokenExpired, login } = useUserContext();
  const settings = useSettings();
  // Open-ended: the Zoom reader forwards fields out of Zoom's own MeetingConfig,
  // so neither field is guaranteed to be present or to be a string.
  const [metadata, setMetadata] = useState<MeetingMetadata>({ userName: "", meetingTopic: "" });
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

  // Holds what the readers have reported so far, and the tab it came from, so
  // that a partial update can be folded into it without `updateMetadata`
  // depending on the `metadata` state and being rebuilt on every change.
  const knownMetadata = useRef<MetadataForTab>({
    source: {},
    metadata: { userName: "", meetingTopic: "" },
  });

  // The tab the panel is showing. Every reader now reports unprompted when its
  // page becomes ready, so without this a meeting-platform tab opened in the
  // background would re-point the panel away from the tab being captured.
  const subjectTabId = useRef<number | undefined>(undefined);

  const updateMetadata = useCallback((newMetadata: any, source: ReportSource = {}) => {
    if (!newMetadata) {
      // A reader that had nothing to say, or a tab with no reader at all.
      return;
    }
    if (source.tabId !== undefined && subjectTabId.current !== undefined
      && source.tabId !== subjectTabId.current) {
      console.log("Ignoring metadata from a tab the panel is not showing", source.tabId);
      return;
    }
    const known = mergeMetadataForTab(knownMetadata.current, newMetadata, source);
    knownMetadata.current = known;
    const merged = known.metadata;

    const detected = platformFromBaseUrl(merged.baseUrl as string | undefined);
    // Left as-is when unrecognised, so a tab change away from a meeting does not
    // clear a platform that is still being captured.
    if (detected !== UNKNOWN_PLATFORM) {
      setPlatform(detected);
    }

    setMetadata(merged as any);
  }, [setMetadata, setPlatform]);

  const fetchMetadata = async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.id) {
      subjectTabId.current = tab.id;
      try {
        // Only the top frame. With `all_frames` enabled every frame holding a
        // reader answers, and Chrome keeps whichever replies first — which may
        // be a frame that has found less than another one has.
        const response = await chrome.tabs.sendMessage(
          tab.id, { action: "FetchMetadata" }, { frameId: 0 },
        );
        console.log("Received response from Metadata query!", response);
        updateMetadata(response, { tabId: tab.id, url: tab.url });
      } catch (error) {
        // No reader in this tab, or it did not answer. The platform stays as it
        // was and the user fills the fields in by hand.
        console.log("No meeting metadata available for this tab", error);
      }
    }
    return {};
  }

  const sendRecordingMessage = useCallback(async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.id) {
      try {
        await chrome.tabs.sendMessage(tab.id, { action: "SendChatMessage", message: settings.recordingMessage });
      } catch (error) {
        // No reader in this tab to post the notice. Neither caller awaits this,
        // so an unhandled rejection is the alternative.
        console.log("Unable to send the chat message to this tab", error);
      }
    }
    return {};
  }, [settings]);

  const sendStopMessage = useCallback(async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.id) {
      try {
        await chrome.tabs.sendMessage(tab.id, { action: "SendChatMessage", message: settings.stopRecordingMessage });
      } catch (error) {
        // No reader in this tab to post the notice. Neither caller awaits this,
        // so an unhandled rejection is the alternative.
        console.log("Unable to send the chat message to this tab", error);
      }
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
          // Scoped to the reporting tab, so one meeting's topic is not carried
          // into the next. Messages from the panel itself have no `sender.tab`.
          updateMetadata(
            request.metadata,
            sender && sender.tab ? { tabId: sender.tab.id, url: sender.tab.url } : {},
          );
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
    // `metadata` is deliberately absent: the handler no longer reads it, and
    // including it re-registered the listener on every reader report.
  }, [currentCall, readyState, muted, paused, activeSpeaker, isTranscribing, setMuted,
    setActiveSpeaker, sendMessage, setPlatform, setIsTranscribing, sendRecordingMessage, updateMetadata
  ]);

  // Follow the user to another meeting tab. Needed because reports from tabs the
  // panel is not showing are ignored, which would otherwise leave the panel
  // stuck on whichever tab was active when it opened. Not while transcribing:
  // the meeting being captured is the one whose details belong on screen.
  useEffect(() => {
    if (!chrome.tabs || !chrome.tabs.onActivated || isTranscribing) {
      return;
    }
    const handleTabActivated = () => { fetchMetadata(); };
    chrome.tabs.onActivated.addListener(handleTabActivated);
    return () => chrome.tabs.onActivated.removeListener(handleTabActivated);
    // Only `isTranscribing`: `fetchMetadata` is redefined on every render and
    // listing it would re-register the listener each time for no benefit.
  }, [isTranscribing]);

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