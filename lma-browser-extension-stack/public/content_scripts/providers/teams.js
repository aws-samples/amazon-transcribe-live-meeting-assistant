console.log("Inside LMA Teams script");

let metadata = {
  baseUrl: window.location.origin
}

const openChatPanel = function () {
  const chatPanelButtons = document.querySelectorAll('[aria-label*="Open chat"]');
  if (chatPanelButtons.length > 0) {
    chatPanelButtons[0].click(); // open the chat panel
  }
}

const sendChatMessage = function (message) {
  const chatInput = document.querySelector('[aria-label="Type a new message"]');
  if (chatInput) {
    chatInput.value = message;
    const inputEvent = new Event('input', { bubbles: true });
    chatInput.dispatchEvent(inputEvent);
    const sendButton = chatInput.closest('div').querySelector('[aria-label="Send"]');
    if (sendButton) {
      sendButton.click();
    }
  }
}

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "FetchMetadata") {
    checkForMeetingMetadata();
  }
  if (request.action === "SendChatMessage") {
    console.log("received request to send a chat message");
    console.log("message:", request.message);
    let chatInput = document.querySelector('[aria-label="Type a new message"]');
    if (!chatInput) {
      openChatPanel();
    }
    sendChatMessage(request.message);
  }
});

const checkForMeetingMetadata = function () {
  const intervalId = setInterval(() => {
    const showMoreButton = document.getElementById('callingButtons-showMoreBtn');
    if (showMoreButton) {
      showMoreButton.click();
    }
    const meetingInfoButton = document.querySelector('[aria-label="Meeting info"]');
    if (meetingInfoButton) {
      meetingInfoButton.click();
    }
    const meetingTitle = document.querySelector('[data-tid="call-title"]');
    if (meetingTitle) {
      metadata.meetingTopic = meetingTitle.innerText;

      const avatarElement = document.querySelector('span[data-tid="me-control-avatar"]');

      if (avatarElement) {
        // Get the value of the aria-label attribute
        const ariaLabel = avatarElement.getAttribute('aria-label');
        // Extract the email from the aria-label using a regular expression
        const emailMatch = ariaLabel.match(/Profile picture of (.*@.*\..*)\./);
        if (emailMatch && emailMatch[1]) {
          const email = emailMatch[1];
          console.log(email);
          metadata.userName =email;
        } else {
          console.log('Email not found in aria-label');
        }
      } else {
        console.log('Element not found');
      }
      
      chrome.runtime.sendMessage({
        action: "UpdateMetadata",
        metadata: metadata
      });
      clearInterval(intervalId); // Stop checking once the element is found
    }
  }, 2000); // Check every 2000 milliseconds (2 seconds)
}

window.onload = function () {

  const muteObserver = new MutationObserver((mutationList) => {
    mutationList.forEach((mutation) => {
      if (mutation.type === 'attributes' && mutation.attributeName === 'title') {
        const muteButton = mutation.target;
        if (muteButton.getAttribute('title').includes('Unmute')) {
          chrome.runtime.sendMessage({ action: "MuteChange", mute: true });
          console.log("Mute detected");
        } else if (muteButton.getAttribute('title').includes('Mute')) {
          chrome.runtime.sendMessage({ action: "MuteChange", mute: false });
          console.log("Unmute detected");
        }
      }
    });
  });

  const muteInterval = setInterval(() => {
    const muteButton = document.querySelector('[aria-label*="Mute"]');
    if (muteButton) {
      muteObserver.observe(muteButton, { attributes: true });
      clearInterval(muteInterval);
    }
  }, 2000);

  checkForMeetingMetadata();

  const activeSpeakerObserver = new MutationObserver((mutationList) => {
    mutationList.forEach((mutation) => {
      if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
        const activeSpeaker = mutation.addedNodes[0].innerText;
        if (activeSpeaker) {
          chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
        }
      }
    });
  });

  const activeSpeakerInterval = setInterval(() => {
    const activeSpeakerElement = document.querySelector('.active-speaker-name');
    if (activeSpeakerElement) {
      activeSpeakerObserver.observe(activeSpeakerElement, { childList: true });
      clearInterval(activeSpeakerInterval);
    }
  }, 2000);
};
