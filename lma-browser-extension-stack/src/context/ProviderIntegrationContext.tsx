import React, { createContext, startTransition, useCallback, useContext, useEffect, useRef, useState } from 'react';
import useWebSocket, { ReadyState } from 'react-use-websocket';
import { useSettings } from './SettingsContext';
import { useUserContext } from './UserContext';
import { WebSocketHook } from 'react-use-websocket/dist/lib/types';

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
  setMuted: (muteValue:boolean) => {},
  paused: false,
  setPaused: (pauseValue:boolean) => {},
  fetchMetadata: () => {},
  startTranscription: (userName:string, meetingTopic:string) => {},
  stopTranscription: () => {},
  metadata: {
    userName: "",
    meetingTopic: ""
  },
  platform: "n/a",
  activeSpeaker: "n/a",
  sendRecordingMessage: () => {}
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

  const dataUrlToBytes = async (dataUrl:string, isMuted:boolean, isPaused:boolean) => {
    const res = await fetch(dataUrl);
    const dataArray = new Uint8Array(await res.arrayBuffer());
    if (isPaused) {
      // mute all channels by sending just zeroes
      return new Uint8Array(dataArray.length);
    } else if (isMuted) {
      // mute only the one channel by mutating the zeroes of only one channel (channel 1)
      for (let i = 2; i < dataArray.length; i += 4) {
        dataArray[i] = 0;
        dataArray[i + 1] = 0;
      }
    }
    return dataArray;
  }
  
  const fetchMetadata = async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { action: "FetchMetadata" });
    }
    return {};
  }

  const sendRecordingMessage = useCallback(async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { action: "SendRecordingMessage", message: settings.recordingMessage });
    }
    return {};
  }, [settings]);

  const getTimestampStr = () => {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0'); // JavaScript months start at 0
    const day = String(now.getDate()).padStart(2, '0');
    const hour = String(now.getHours()).padStart(2, '0');
    const minute = String(now.getMinutes()).padStart(2, '0');
    const second = String(now.getSeconds()).padStart(2, '0');
    const millisecond = String(now.getMilliseconds()).padStart(3, '0');
    const formattedDate = `${year}-${month}-${day}-${hour}:${minute}:${second}.${millisecond}`;
    return formattedDate;
  }

  const startTranscription = useCallback(async (userName: string, meetingTopic: string) => {
    if (checkTokenExpired()) {
      login();
      return;
    }

    setShouldConnect(true);
    let callMetadata = {
      callEvent: 'START',
      agentId: userName,
      fromNumber: '+9165551234',
      toNumber: '+8001112222',
      callId: `${meetingTopic} - ${getTimestampStr()}`,
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
  }, [readyState,shouldConnect, isTranscribing, paused, getWebSocket, sendMessage, setPaused]);

  useEffect(() => {
    if (chrome.runtime) {
      const handleRuntimeMessage = async (request:any, sender:any, sendResponse:any) => {
        if (request.action === "TranscriptionStopped") {
          stopTranscription();
        } else if (request.action === "UpdateMetadata") {
          if (request.metadata.baseUrl && request.metadata.baseUrl === "https://app.zoom.us") {
            setPlatform("Zoom");
          } else if (request.metadata.baseUrl && request.metadata.baseUrl === "https://app.chime.aws") {
            setPlatform("Amazon Chime");
          }
          setMetadata(request.metadata);
        } else if (request.action === "SamplingRate") {
          // This event should only bubble up once at the start of recording in the injected code
          currentCall.samplingRate = request.samplingRate;
          currentCall.callEvent = 'START';
          sendMessage(JSON.stringify(currentCall));
          setIsTranscribing(true);
          sendRecordingMessage();
        } else if (request.action === "AudioData") {
          if (readyState === ReadyState.OPEN)
          {
            let audioData = await dataUrlToBytes(request.audio, muted, paused);
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
  }, [currentCall, metadata, readyState, muted, paused, activeSpeaker, setMuted,
    setActiveSpeaker, sendMessage, setMetadata, setPlatform, setIsTranscribing, sendRecordingMessage
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