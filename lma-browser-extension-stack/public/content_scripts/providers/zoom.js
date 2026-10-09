/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
console.log("Inside LMA Zoom script");

let meetingConfig = {};

/**
 * What this reader reports to the panel.
 *
 * The name and topic come from Zoom's own `MeetingConfig` page object, which may
 * not be there (see zoom-injection.js). `baseUrl` is taken from the tab instead,
 * so the panel can always name the platform; it is set last when MeetingConfig
 * is merged in, because the tab's own origin is the more reliable of the two.
 */
let metadata = {
  baseUrl: window.location.origin
};

const reportMetadata = function () {
  console.log("Sending Metadata:", metadata);
  // The side panel may not be open, in which case there is no receiver. That is
  // not a failure worth surfacing: the panel asks again when it opens.
  try {
    const sent = chrome.runtime.sendMessage({
      action: "UpdateMetadata",
      metadata: metadata
    });
    if (sent && typeof sent.catch === 'function') {
      sent.catch(() => { });
    }
  } catch (error) {
    console.log("Unable to send metadata; the panel is not listening.");
  }
}

/************** Helper functions ***************/
const getNameForVideoAvatar = function (element) {
  var speakerName = "n/a";
  var footerSpan = element.querySelector('.video-avatar__avatar-footer span[role="none"]');
  if (footerSpan) {
    speakerName = footerSpan.innerText;
  } else {
    // Fall back to avatar name checks
    var avatarEl = element.querySelector('.video-avatar__avatar-name');
    if (avatarEl) {
      speakerName = avatarEl.innerText;
    } else {
      avatarEl = element.querySelector('.video-avatar__avatar-img');
      if (avatarEl) speakerName = avatarEl.alt;
    }
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
    // Always answer. An unanswered request leaves the panel with nothing, and
    // the reply carries the tab's origin even when MeetingConfig never appeared.
    sendResponse(metadata);
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
  if (event.origin !== window.location.origin) return;
  
  if (event.data.type && (event.data.type == "MeetingConfig")) {
    console.log("received value from page: ", event.data.value);
    meetingConfig = event.data.value;
    metadata = Object.assign({}, meetingConfig, { baseUrl: window.location.origin });
    reportMetadata();
  }
});

// Report the platform without waiting on MeetingConfig, which may never arrive.
reportMetadata();
