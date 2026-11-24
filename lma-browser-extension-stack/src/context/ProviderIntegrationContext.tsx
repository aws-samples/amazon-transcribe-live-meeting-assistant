/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable @typescript-eslint/no-empty-function */
import React, { createContext, startTransition, useCallback, useContext, useEffect, useRef, useState } from 'react';
import useWebSocket, { ReadyState } from 'react-use-websocket';
import { useSettings } from './SettingsContext';
import { useUserContext } from './UserContext';
import { WebSocketHook } from 'react-use-websocket/dist/lib/types';
import { applyMuteAndPause, formatTimestamp, MeetingMetadata, MetadataForTab, mergeMetadataForTab, platformFromBaseUrl, ReportSource, UNKNOWN_PLATFORM } from '../lib/capture';
import { refusalMessage, reopenMeeting } from '../lib/continue-meeting';
import { CONSENT_MODE_OPT_IN, OBJECTION_NOT_SENT, captureStopped, consentPayload, emptyStep, parseServerFrame, transcriberUrl, withoutConsent } from '../lib/consent';
import { VIDEO_OFF, VideoState, createPanelVideo } from '../lib/panel-video';

// A control frame of the WebSocket streaming API (docs/websocket-streaming-api.md).
// The meeting-audio speaker is reported as it changes, by SPEAKER_CHANGE, so the
// frame names only the local one.
type Call = {
  callEvent: string,
  // Which client captured the meeting, stored on its record as its capture source.
  clientType: string,
  localSpeakerName: string,
  callId: string,
  samplingRate: number,
  activeSpeaker: string,
  // Only to continue an existing meeting: the capture session reopenMeeting
  // allowed, with the meeting's own ID as callId.
  sessionNumber?: number,
  // The consent step (lib/consent.ts), sent with every START.
  consent?: Record<string, unknown>,
}

const initialIntegration = {
  currentCall: {} as Call,
  isTranscribing: false,
  muted: false,
  setMuted: (muteValue: boolean) => { },
  paused: false,
  setPaused: (pauseValue: boolean) => { },
  fetchMetadata: () => { },
  // `continueMeeting`: the meeting ID or link of an existing meeting to continue.
  // `consent`: the consent step's START field (lib/consent.ts consentPayload).
  // `video`: whether to also record the meeting tab as screen video (lib/panel-video.ts).
  startTranscription: async (user: any, userName: string, meetingTopic: string, continueMeeting?: string, consent?: Record<string, unknown>, video?: boolean) => { },
  // Why capture did not start, or stopped: a reopenMeeting refusal, or the
  // transcriber's. Empty otherwise.
  startError: "",
  setStartError: (message: string) => { },
  stopTranscription: () => { },
  metadata: { userName: "", meetingTopic: "" } as MeetingMetadata,
  platform: "n/a",
  activeSpeaker: "n/a",
  sendRecordingMessage: () => { },
  // The last CONSENT_STATE frame of this capture, or null.
  consentFrame: null as Record<string, any> | null,
  // Posts text in the meeting chat of the active tab; false when it could not be sent.
  postInMeetingChat: async (text: string) => false as boolean,
  // A participant objected: tells the transcriber, which ends the meeting, and stops capturing.
  reportObjection: (participantName: string) => { },
  // This capture's screen video, and Stop video.
  videoState: VIDEO_OFF as VideoState,
  stopVideo: () => { },
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
  const [startError, setStartError] = useState("");
  const [consentFrame, setConsentFrame] = useState<Record<string, any> | null>(null);
  // Set when the transcriber has ended the meeting (its consent, or an
  // objection reported here): stopping then sends no END.
  const skipEndRef = useRef(false);

  // Screen video of the meeting tab (lib/panel-video.ts): asked for at
  // startTranscription, started once START is sent and consent allows capture.
  const wantVideoRef = useRef(false);
  // The tab whose recorder captures this meeting.
  const capturedTabRef = useRef<number | undefined>(undefined);
  const [videoState, setVideoState] = useState<VideoState>(VIDEO_OFF);
  const userRef = useRef(user);
  userRef.current = user;
  const panelVideoRef = useRef<ReturnType<typeof createPanelVideo> | null>(null);
  if (panelVideoRef.current === null) {
    panelVideoRef.current = createPanelVideo({
      openSocket: async () => new WebSocket(transcriberUrl(settings.wssEndpoint as string, userRef.current)),
      // To the recorder in the captured tab only, as StartTranscription is
      // sent: every open tab has a recorder.
      sendToRecorder: (message) => {
        const tabId = capturedTabRef.current;
        if (tabId !== undefined && chrome.tabs) chrome.tabs.sendMessage(tabId, message).catch(() => undefined);
      },
      onState: (state) => setVideoState(state),
      log: (message) => console.log(`LMA video: ${message}`),
    });
  }
  const panelVideo = panelVideoRef.current;
  // The meeting ID or link to continue, from startTranscription until the
  // capture has started.
  const continueMeetingRef = useRef<string | undefined>(undefined);
  // Whether this capture's START has been sent: the tab's audio is held back
  // until it has, since the transcriber drops audio that arrives before START
  // (the tab starts sending once it reports its sampling rate, and a continued
  // meeting is reopened between the two).
  const startSentRef = useRef(false);

  const { sendMessage, readyState, getWebSocket } = useWebSocket(settings.wssEndpoint as string, {
    queryParams: {
      authorization: `Bearer ${user.access_token}`,
      id_token: `${user.id_token}`,
      refresh_token: `${user.refresh_token}`
    },
    onOpen: (event) => {
      console.log(event);
    },
    onMessage: (event) => {
      // A START refused for the meeting it names, or capture the transcriber
      // stopped; it then closes the connection.
      const refusal = refusalMessage(event.data);
      if (refusal) setStartError(refusal);
      const frame = parseServerFrame(event.data);
      if (frame?.event === 'CONSENT_STATE') {
        setConsentFrame(frame);
        // The transcriber has ended the meeting and closes the connection:
        // nothing more is sent for it, END included.
        if (captureStopped(frame)) skipEndRef.current = true;
        if (frame.status === "granted" && frame.captureAllowed !== false) panelVideoRef.current?.granted();
      }
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

  // True when the tab's reader was asked to post, not that the notice appeared:
  // the user's answer in the panel decides whether it counts as posted.
  const postInMeetingChat = useCallback(async (text: string) => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!tab || !tab.id) return false;
    try {
      await chrome.tabs.sendMessage(tab.id, { action: "SendChatMessage", message: text });
      return true;
    } catch (error) {
      console.log("Unable to send the chat message to this tab", error);
      return false;
    }
  }, []);

  const startTranscription = useCallback(async (user: any, userName: string, meetingTopic: string, continueMeeting?: string, consent?: Record<string, unknown>, video?: boolean) => {
    wantVideoRef.current = video === true;
    if (await checkTokenExpired(user)) {
      login();
      return;
    }
    setStartError("");
    // Continuing a meeting: reopened once the tab's capture has started, just
    // before START (the SamplingRate message below), so a capture that never
    // starts leaves no session allowed (reopenMeeting holds the meeting for an
    // allowed session for 15 minutes).
    continueMeetingRef.current = continueMeeting;
    startSentRef.current = false;
    skipEndRef.current = false;
    setConsentFrame(null);

    setShouldConnect(true);
    const callMetadata: Call = {
      callEvent: 'START',
      clientType: 'BROWSER_EXTENSION',
      localSpeakerName: userName,
      callId: `${meetingTopic} - ${formatTimestamp(new Date())}`,
      samplingRate: 8000,
      activeSpeaker: 'n/a',
      // Always sent, as mode off when the settings were not read, so an opt-in
      // deployment refuses a START this panel did not ask about.
      consent: consent ?? consentPayload(null, emptyStep(), { lmaUser: userName, meetingName: meetingTopic, noticeText: '' }),
    }

    setCurrentCall(callMetadata);

    try {
      if (chrome.runtime) {
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (tab.id) {
          capturedTabRef.current = tab.id;
          const response = await chrome.tabs.sendMessage(tab.id, { action: "StartTranscription", video: wantVideoRef.current });
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
      if (readyState === ReadyState.OPEN && !skipEndRef.current) {
        currentCall.callEvent = 'END';
        sendMessage(JSON.stringify(withoutConsent(currentCall)));
        getWebSocket()?.close();
      }
      setShouldConnect(false);
      setIsTranscribing(false);
      panelVideoRef.current?.end();
      setPaused(false);
      sendStopMessage();
    }
  }, [readyState, shouldConnect, isTranscribing, paused, setIsTranscribing, getWebSocket, sendMessage, setPaused, sendStopMessage, sendRecordingMessage]);

  const reportObjection = useCallback((participantName: string) => {
    if (readyState === ReadyState.OPEN) {
      sendMessage(JSON.stringify({
        callEvent: 'CONSENT_OBJECTION',
        ...(participantName ? { participantName } : {}),
        source: 'voice',
      }));
      // The transcriber ends the meeting on the objection and closes the
      // connection, so no END is sent; capture stops here at once. The panel
      // says so now, as the connection closes before the transcriber's answer.
      skipEndRef.current = true;
      setConsentFrame({ event: 'CONSENT_STATE', status: 'objected', captureAllowed: false });
    } else {
      // The extension does not reconnect, so the objection cannot reach the
      // transcriber: capture stops, and the user is told to record it.
      setStartError(OBJECTION_NOT_SENT);
    }
    stopTranscription();
  }, [readyState, sendMessage, stopTranscription]);

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
          const continueMeeting = continueMeetingRef.current;
          continueMeetingRef.current = undefined;
          if (continueMeeting !== undefined) {
            const result = await reopenMeeting(settings.graphqlEndpoint, user.id_token, continueMeeting);
            if (result.error !== undefined) {
              setStartError(result.error);
              chrome.runtime.sendMessage({ action: "StopTranscription" });
              setShouldConnect(false);
              return;
            }
            currentCall.callId = result.session.callId;
            currentCall.sessionNumber = result.session.sessionNumber;
          }
          currentCall.samplingRate = request.samplingRate;
          currentCall.callEvent = 'START';
          sendMessage(JSON.stringify(currentCall));
          startSentRef.current = true;
          setIsTranscribing(true);
          panelVideo.begin(currentCall.callId, wantVideoRef.current, (currentCall.consent as { mode?: string } | undefined)?.mode === CONSENT_MODE_OPT_IN);
          sendRecordingMessage();
        } else if (request.action === "AudioData") {
          if (readyState === ReadyState.OPEN && startSentRef.current) {
            const audioData = await dataUrlToBytes(request.audio, muted, paused);
            sendMessage(audioData);
          }
        } else if (request.action === "ActiveSpeakerChange") {
          currentCall.callEvent = 'SPEAKER_CHANGE';
          currentCall.activeSpeaker = request.active_speaker;
          setActiveSpeaker(request.active_speaker);
          sendMessage(JSON.stringify(withoutConsent(currentCall)));
        } else if (["VideoStarted", "VideoChunk", "VideoStopped", "VideoUnavailable"].includes(request.action)) {
          // Only from the recorder in the tab being captured.
          if (sender && sender.tab && sender.tab.id === capturedTabRef.current) {
            panelVideo.onRecorderMessage(request);
          }
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
    setActiveSpeaker, sendMessage, setPlatform, setIsTranscribing, sendRecordingMessage, updateMetadata,
    settings, user
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
      activeSpeaker, sendRecordingMessage, startError, setStartError,
      consentFrame, postInMeetingChat, reportObjection,
      videoState, stopVideo: panelVideo.stop
    }}>
      {children}
    </IntegrationContext.Provider>
  );
}
export function useIntegration() {
  return useContext(IntegrationContext);
}
export default IntegrationProvider;