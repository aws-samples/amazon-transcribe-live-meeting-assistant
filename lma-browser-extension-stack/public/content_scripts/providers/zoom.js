console.log("Inside LMA Zoom script");

let meetingConfig = {};

/************** Helper functions ***************/
const getNameForVideoAvatar = function (element) {
  var speakerName = "n/a";
  var avatarEl = element.querySelector('.video-avatar__avatar-name');
  if (avatarEl)  speakerName = avatarEl.innerText;
  else (avatarEl === undefined)
  {
    var avatarEl = element.querySelector('.video-avatar__avatar-img');
    if (avatarEl) speakerName = avatarEl.alt;
  }
  return speakerName;
}

/************ This is for handling people joining and leaving the meeting **************/
const handleParticipantChange = function (summaries) {
  console.log("Participant change detected");
  console.log(summaries);
  summaries.forEach(function (summary) {
    summary.added.forEach(function(newEl) {
      const speakerName = getNameForVideoAvatar(newEl);
      console.log("Added Speaker", speakerName);
    });
    summary.removed.forEach(function(removedEl) {
      const speakerName = getNameForVideoAvatar(removedEl);
      console.log("Removed Speaker", speakerName);
    });
  });
}

var observer = new MutationSummary({
  callback: handleParticipantChange,
  queries: [
    { element: '.video-avatar__avatar' }
  ]
});

/************ This is for detecting active speaker **************/
const handleActiveSpeakerChanges = function (summaries) {
  console.log("Participant change detected");
  summaries.forEach(function (summary) {
    summary.added.forEach(function (newEl) {
      const speakerName = getNameForVideoAvatar(newEl);
      console.log("Active Speaker changed:", speakerName);
      chrome.runtime.sendMessage({action: "ActiveSpeakerChange", active_speaker: speakerName});
    });
  });
}

var observer = new MutationSummary({
  callback: handleActiveSpeakerChanges,
  queries: [
    { element: '.speaker-active-container__video-frame' },
    { element: '.speaker-bar-container__video-frame--active'},
    { element: '.gallery-video-container__video-frame--active'},
  ]
});

/*********** Detecting mute or unmute *************/
const handleMuteChanges = function (summaries) {
  console.log("Mute change detected");

  let isMuted = false;
  for (let element of document.getElementsByClassName('footer-button-base__button-label')) {
    if (element.innerText === "Unmute") {
      isMuted = true;
    }
  }
  chrome.runtime.sendMessage({action: "MuteChange", mute: isMuted});
};

var muteObserver = new MutationSummary({
  callback: handleMuteChanges,
  queries: [
    { element: '.video-avatar__avatar-footer--view-mute-computer' },
    { element: '.footer-button-base__img-layer' },
    { element: '.footer-button-base__button-label' }
  ]
});


const openChatPanel = function () {
    const chatPanelButtons = document.querySelectorAll('[aria-label*="open the chat panel"]');
    if (chatPanelButtons.length > 0) {
      chatPanelButtons[0].click(); // open the attendee panel
    }
}

const sendChatMessage = function (message) {
  const chatPanelButtons = document.getElementsByClassName("chat-rtf-box__send");
  if (chatPanelButtons.length > 0) {
    const outerTextBox = document.getElementsByClassName("chat-rtf-box__editor-outer");
    if (outerTextBox.length > 0) {
      
      const innerTextBox = outerTextBox[0].querySelectorAll("p");
      if (innerTextBox.length > 0) {
        innerTextBox[0].innerText = message;
      }
    }
    setTimeout(() => {
      chatPanelButtons[0].click();
    }, 250);
  }
}

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "FetchMetadata") {
    console.log("Received request to send meeting config");
    if (Object.keys(meetingConfig).length > 0) {
      console.log("Sending meeting config to extension");
      sendResponse(meetingConfig);
    }
  }
  else if (request.action === "SendChatMessage") {
    console.log("received request to send a chat message");
    console.log("message:", request.message);
    let chatWindow = document.getElementsByClassName("chat-rtf-box__editor-outer");
    if (chatWindow.length === 0) {
      openChatPanel();
    }
    setTimeout(() => {
      sendChatMessage(request.message);
    }, 500);
  }
});

function injectScript(file) {
  const script = document.createElement('script');
  script.src = chrome.runtime.getURL(file);
  script.onload = function () {
    script.remove();
  }
    
  const target = document.head || document.Element;
  if (target) {
    target.appendChild(script);
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      (document.head || document.documentElement).appendChild(script);
    });
  }  
}

injectScript('content_scripts/providers/zoom-injection.js');

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  
  if (event.data.type && (event.data.type == "MeetingConfig")) {
    console.log("received value from page: ", event.data.value);
    meetingConfig = event.data.value;
    chrome.runtime.sendMessage({ action: "UpdateMetadata", metadata: event.data.value });
  }
});