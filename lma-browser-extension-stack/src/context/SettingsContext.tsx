/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
import { createContext, useContext, useState } from 'react';

type Settings = {
  wssEndpoint: string,
  clientId: string,
  cognitoDomain: string,
  cloudfrontEndpoint: string,
  recordingDisclaimer: string,
  recordingMessage: string,
  stopRecordingMessage: string
}

const initialSettings = {} as Settings;
const SettingsContext = createContext(initialSettings);

function SettingsProvider({ children }: any) {
  let settingsJson = {} ;
  const xhr = new XMLHttpRequest();
  xhr.open('GET', 'lma_config.json', false);
  xhr.send();

  if (xhr.status === 200) {
    // Success!
    settingsJson = JSON.parse(xhr.responseText);
  }

  const [settings, setSettings] = useState(settingsJson as Settings);
  
  // Load settings from a file
  /*useEffect(() => {
    const loadSettings = async () => {
      const response = await fetch('lma_config.json');
      const data = await response.json();
      setSettings(data);
    };

    loadSettings();
  }, []);*/

  return (
    <SettingsContext.Provider value={ settings }>
      {children}
    </SettingsContext.Provider>
  );
}
export function useSettings() { 
  return useContext(SettingsContext);
}
export default SettingsProvider;