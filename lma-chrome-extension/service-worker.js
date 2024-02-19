
let config;

let callEvent = {
  callId: crypto.randomUUID(),
  agentId: 'LMA',
  fromNumber: '+9165551234',
  toNumber: '+8001112222',
  samplingRate: undefined,
};

/************* Sidepanel settings ********/
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error(error));
  
const loadConfig = async () => {
  try {
    const response = await fetch('lma_config.json');
    config = await response.json();
    console.log("Loaded config");
  } catch (error) {
    console.error('failed to load config', error);
  }
}

/************* Helper functions *************/
async function dataUrlToBytes(dataUrl) {
  const res = await fetch(dataUrl);
  return new Uint8Array(await res.arrayBuffer());
}

/******** Auth tokens ***********/

const isTokenExpired = (jwtToken) => {
  const [, payload] = jwtToken.split('.');
  const { exp: expires } = JSON.parse(atob(payload));
  if (typeof expires === 'number') {
    let expiryDate = new Date(expires * 1000);
    console.log("expiry:", expiryDate);
    return (expiryDate < new Date());
  }
  return true;
}

const storeAuthTokens = async (tokens) => {
  let response = await chrome.storage.local.set({ authTokens: tokens });
  console.log("Tokens stored.");
}

const removeAuthTokens = async () => {
  await chrome.storage.local.remove(['authTokens']);
  console.log("removed auth tokens.");
}

const retrieveAuthTokensFromStorage = async () => {
  let result = await chrome.storage.local.get(['authTokens']);
  if (result.authTokens) {
    console.log("token:", result.authTokens);
    let isExpired = isTokenExpired(result.authTokens.access_token);
    if (isExpired) {
      await removeAuthTokens();
      return undefined;
    }
    console.log("retrieved auth tokens"); 
    return result.authTokens;
  } else {
    console.log("No auth tokens");
    return undefined;
  }
}

const exchangeCodeForToken = async (code) => {
  const tokenEndpoint = `https://${config.cognitoDomain}/oauth2/token`
  const params = new URLSearchParams();
  params.append('grant_type', 'authorization_code');
  params.append('client_id', config.clientId);
  params.append('redirect_uri', `https://${chrome.runtime.id}.chromiumapp.org/`);
  params.append('code', code);

  try {
    const response = await fetch(tokenEndpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded'
      }, body: params
    });

    if (!response.ok) {
      throw new Error(`HTTP ERROR! status: ${response.status}`);
    }

    const data = await response.json();
    console.log('Tokens!', data);
    chrome.runtime.sendMessage({ action: "Authenticated" });
    storeAuthTokens(data);

    return data;
  } catch (error) {
    console.error('error exchanging code for token', error);
    throw error;
  }
}

const startAuthFlow = async () => {
  console.log("start auth flow");
  chrome.identity.launchWebAuthFlow({
    url: `https://${config.cognitoDomain}/login?response_type=code&client_id=${config.clientId}&redirect_uri=https://${chrome.runtime.id}.chromiumapp.org/&scope=email+openid+profile`,
    interactive:true
  }, function (redirectURL) {
    console.log("WE GOT A RESPONSE FROM AUTH!", redirectURL);
    let url = new URL(redirectURL);
    let authorizationCode = url.searchParams.get("code");
    exchangeCodeForToken(authorizationCode);
  });
}

/*********** Messaging ***********/
chrome.runtime.onMessage.addListener(async function (request, sender, sendResponse) {
  //console.log("Received new message:", request);
  if (request.action === "StartTranscription") {
    startTranscription();
  } else if (request.action === "StopTranscription") {
    stopTranscription();
  } else if (request.action === "ActiveSpeakerChange") {
    console.log("active speaker change from within service-worker");
    if (websocket !== undefined && websocket.readyState === WebSocket.OPEN) {
      //send speaker change here
      let speakerChange = {
        callEvent: 'SPEAKER_CHANGE',
        callId: callEvent.callId,
        activeSpeaker: request.active_speaker
      }
      websocket.send(JSON.stringify(speakerChange));
    }
  } else if (request.action === "AudioData") {
    if (websocket !== undefined && websocket.readyState === WebSocket.OPEN) {
      //send audio here
      //console.log("sending audio to websocket");      
      let audioData = await dataUrlToBytes(request.audio);
      websocket.send(audioData);
    }
  } else if (request.action === "SamplingRate") {
    callEvent.samplingRate = request.samplingRate;
    startTranscription();
  } else if (request.action === "Authenticate") {
    let authTokens = await retrieveAuthTokensFromStorage();
    if (authTokens) {
      // validate JWT and refresh if needed
      chrome.runtime.sendMessage({ action: "Authenticated" });
    }
    else {
      startAuthFlow();
    }
  }
});


/********** Browser WebSocket ***********/
let websocket;

const wsOnOpen = function wsOnOpen(event) {
  console.log("Websocket Opened", event);
  // send start call event

  //generate new call id & set the event to start
  callEvent.callId = crypto.randomUUID();
  callEvent.callEvent = 'START';

  chrome.runtime.sendMessage({ action: "NewCallId", callId: callEvent.callId });

  if (websocket) {
    console.log("sending new call details", callEvent);
    websocket.send(JSON.stringify(callEvent));
  }
}

const wsOnMessage = function wsOnMessage(event) {
  console.log("Websocket message received", event);
}

const wsOnClose = function wsOnClose(event) {
  console.log("Websocket message closed", event);
  chrome.runtime.sendMessage({ action: "StopTranscription" });
  websocket = undefined;
}

const wsOnError = function wsOnError(event) {
  console.log("Websocket error", event);
}

const startTranscription = async function startTranscription() {
  if (websocket) {
    console.log("Websocket is already open");
    return;
  }
  
  if (callEvent.samplingRate === undefined) {
    console.log("we do not yet know sampling rate");
    return;
  }

  let tokens = await retrieveAuthTokensFromStorage();
  if (!tokens) {
    console.log("not authorized");
    return;
  }

  console.log("Opening websocket!");

  // we passed tests, lets do it.
  let connectionUrl = `${config.wssEndpoint}?authorization=Bearer%20${encodeURIComponent(tokens.access_token)}`;
  websocket = new WebSocket(connectionUrl);
  websocket.onopen = wsOnOpen;
  websocket.onclose = wsOnClose;
  websocket.onerror = wsOnError;
  websocket.onmessage = wsOnMessage;
}

const stopTranscription = async function stopTranscription() {
  if (websocket) {
    callEvent.callEvent = 'STOP';
    await websocket.send(callEvent);

    await websocket.close(1000, "Closed");
    websocket = undefined;
  }
}

loadConfig();