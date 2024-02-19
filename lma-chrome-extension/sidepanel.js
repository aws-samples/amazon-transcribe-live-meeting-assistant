/******** On Load **********/
window.addEventListener("load", (event) => {
  console.log("sidepanel is fully loaded");
  authenticate();
});

/*********** Messaging ***********/
chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  
  if (request.action === "ActiveSpeakerChange") {
    document.getElementById("currentSpeaker").innerHTML = request.active_speaker;
  }
  if (request.action === "NewCallId") {
    document.getElementById("currentCallId").innerHTML = request.callId;
  }

  if (request.action === "UserStoppedRecording") {
    stopTranscription();
  }
  if (request.action === "Authenticated") {
    console.log("sidepanel authenticated");
    document.getElementById("loginBtn").style.visibility = 'hidden';
    document.getElementById("logoutBtn").style.visibility = 'visible';
  }
});


/*********** Audio Capture/Transcription ***********/
let audioCaptureEnabled = false;

const startTranscription = async () => {
  document.getElementById("toggleTranscriptionBtn").innerText = "Stop Transcription";

  // send the worker a message to start streaming
  chrome.runtime.sendMessage({ action: "StartTranscription" });
}

const stopTranscription = async () => {
  document.getElementById("toggleTranscriptionBtn").innerText = "Start Transcription";

  // send the worker a message to start streaming
  chrome.runtime.sendMessage({ action: "StopTranscription" });
  // tell the tab to start streaming
  const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  const response = await chrome.tabs.sendMessage(tab.id, { action: "StopTranscription" });
}

const toggleTranscription = async (params) => {
  audioCaptureEnabled = !audioCaptureEnabled;
  if (audioCaptureEnabled) {
    await startTranscription();    
  } else {
    await stopTranscription();
  }
}
document.getElementById("toggleTranscriptionBtn").addEventListener("click", toggleTranscription);

const authenticate = async (params) => {
  chrome.runtime.sendMessage({ action: "Authenticate" });
}
document.getElementById("loginBtn").addEventListener("click", authenticate);