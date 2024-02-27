import React, { createContext, startTransition, useContext, useEffect, useRef, useState } from 'react';
import useWebSocket from 'react-use-websocket';
import { useSettings } from './SettingsContext';
import { useUserContext } from './UserContext';
import { WebSocketHook } from 'react-use-websocket/dist/lib/types';

type Call = {
  callEvent: string, 
  agentId: string,
  fromNumber: string,
  toNumber: string,
  callId: string,
  samplingRate: number
}

const initialIntegration = {
  isTranscribing: false,
  fetchMetadata: () => {},
  startTranscription: (userName:string, meetingTopic:string) => {},
  stopTranscription: () => {},
  metadata: {
    userName: "",
    meetingTopic: ""
  },
  platform: "n/a"
};
const IntegrationContext = createContext(initialIntegration);

function IntegrationProvider({ children }: any) {

  const [currentCall, setCurrentCall] = useState({} as Call);
  const { user } = useUserContext();
  const settings = useSettings();
  const [metadata, setMetadata] = useState({
    userName: "",
    meetingTopic: ""
  });
  const [platform, setPlatform] = useState("n/a");
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [shouldConnect, setShouldConnect] = useState(false); 

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

  const dataUrlToBytes = async (dataUrl:string) => {
    const res = await fetch(dataUrl);
    return new Uint8Array(await res.arrayBuffer());
  }
  
  const fetchMetadata = async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { action: "FetchMetadata" });
    }
    return {};
  }

  const getTimestampStr = () => {
    const now = new Date();
    const year = now.getFullYear();
    const month = now.getMonth() + 1; // JavaScript months start at 0
    const day = now.getDate();
    const hour = now.getHours();
    const minute = now.getMinutes();
    const second = now.getSeconds();
    const millisecond = now.getMilliseconds();

    const formattedDate = `${year}-${month}-${day}-${hour}:${minute}:${second}.${millisecond}`;

    console.log(formattedDate);
    return formattedDate;
  }

  const startTranscription = async (userName:string, meetingTopic:string) => {
    setShouldConnect(true);
    let callMetadata = {
      callEvent: 'START',
      agentId: userName,
      fromNumber: '+9165551234',
      toNumber: '+8001112222',
      callId: `${meetingTopic}-${getTimestampStr()}`,
      samplingRate: 8000,
    };
    
    setCurrentCall(callMetadata);
   
    if (chrome.runtime) {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (tab.id) {
        chrome.tabs.sendMessage(tab.id, { action: "StartTranscription" });
        // We send a message here, but not actually start the stream until we receive a new message with the sample rate.
      }
    }
  }

  const stopTranscription = () => {
    if (chrome.runtime) {
      chrome.runtime.sendMessage({ action: "StopTranscription" });
    }
    getWebSocket()?.close();
    setShouldConnect(false);
    setIsTranscribing(false);
  }

  useEffect(() => {
    if (chrome.runtime) {
      const handleRuntimeMessage = async (request:any, sender:any, sendResponse:any) => {
        if (request.action === "TranscriptionStopped") {
          setIsTranscribing(false);
        } else if (request.action === "UpdateMetadata") {
          if (request.metadata.baseUrl && request.metadata.baseUrl === "https://app.zoom.us") {
            setPlatform("Zoom");
          }
          setMetadata(request.metadata);
        } else if (request.action === "SamplingRate") {
          setCurrentCall((callState) => {
            callState.samplingRate = request.samplingRate;
            sendMessage(JSON.stringify(callState));
            return callState;
          });
          setIsTranscribing(true);
        } else if (request.action === "AudioData") {
          let audioData = await dataUrlToBytes(request.audio);
          sendMessage(audioData);
        }
      };
      chrome.runtime.onMessage.addListener(handleRuntimeMessage); 
      // Clean up the listener when the component unmounts
      return () => chrome.runtime.onMessage.removeListener(handleRuntimeMessage);
    }
  }, []);

  return (
    <IntegrationContext.Provider value={{ isTranscribing, fetchMetadata, startTranscription, stopTranscription, metadata, platform }}>
      {children}
    </IntegrationContext.Provider>
  );
}
export function useIntegration() { 
  return useContext(IntegrationContext);
}
export default IntegrationProvider;