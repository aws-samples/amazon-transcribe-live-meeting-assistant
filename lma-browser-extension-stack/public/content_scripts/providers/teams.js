console.log("Inside LMA Teams script");

let metadata = {
  baseUrl: window.location.origin
}

const openChatPanel = function () {
  const chatPanelButton = document.getElementById('chat-button');
  // click chatPanelButton if found
  if (chatPanelButton) {
    chatPanelButton.click();
  }
}

const activeRingClass = "fui-Primitive ___19upu4n";
const inactiveRingClass = "fui-Primitive ___s78zj80";

const sendChatMessage = function (message) {
  const findChatInputAndSend = function () {
    try {
      const chatInputDiv = document.querySelector('div[id^="new-message-"]');
      if (chatInputDiv) {
        // Focus and click the div (optional, if needed)
        chatInputDiv.focus();
        chatInputDiv.click();

        // Remove any existing content inside the div
        while (chatInputDiv.firstChild) {
          chatInputDiv.removeChild(chatInputDiv.firstChild);
        }

        // Create a new <p> element with the provided message
        const newParagraph = document.createElement('p');
        newParagraph.setAttribute('data-placeholder', 'Type a message');
        newParagraph.textContent = message;

        // Add the new <p> element to the chatInputDiv
        chatInputDiv.appendChild(newParagraph);

        // Click the send button immediately after appending the new paragraph
        const sendButton = document.querySelector('button[data-tid="newMessageCommands-send"]');
        if (sendButton) {
          sendButton.click();
          console.log('Send button found and clicked');
        } else {
          console.error('Send button not found or disabled');
        }
      } else {
        console.error('Chat input not found, retrying in 1 second...');
        setTimeout(findChatInputAndSend, 1000); // Retry after 1 second
      }
    } catch (error) {
      console.error('Error in findChatInputAndSend:', error);
      setTimeout(findChatInputAndSend, 1000); // Retry after 1 second if there's an error
    }
  };

  try {
    findChatInputAndSend();
  } catch (error) {
    console.error('Error in sendChatMessage:', error);
    setTimeout(() => sendChatMessage(message), 1000); // Retry after 1 second if there's an error
  }
};

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "FetchMetadata") {
    checkForMeetingMetadata();
  }
  if (request.action === "SendChatMessage") {
    console.log("received request to send a chat message");
    console.log("message:", request.message);
    let chatInput = document.querySelector('p[data-placeholder="Type a message"]');
    if (!chatInput) {
      openChatPanel();
    }
    sendChatMessage(request.message);
  }
});

const checkForMeetingMetadata = function () {
  var displayName;
  const intervalId = setInterval(() => {
    if (!displayName) {
      //get the user
      const avatarButton = document.querySelector('button[data-tid="me-control-avatar-trigger"]');
      // Check if the button element is found
      if (avatarButton) {
        // Simulate a click on the button
        avatarButton.click();
        console.log('Button clicked');

        // Query the span element using data-tid attribute
        const displayNameSpan = document.querySelector('span[data-tid="me-control-displayname"]');

        // Check if the span element is found
        if (displayNameSpan) {
          // Get the text content of the span element
          displayName = displayNameSpan.textContent;
          console.log('Display Name:', displayName);
          metadata.userName = displayName;
        } else {
          console.log('Span element with data-tid="me-control-displayname" not found.');
        }
      } else {
        console.log('Button with data-tid="me-control-avatar-trigger" not found.');
      }
    }

    const showMoreButton = document.getElementById('callingButtons-showMoreBtn');
    if (showMoreButton) {
      showMoreButton.click();
    }
    const meetingInfoButton = document.querySelector('[aria-label="Meeting info"]');
    if (meetingInfoButton) {
      meetingInfoButton.click();
    }
    const meetingTitle = document.querySelector('[data-tid="call-title"]');
    if (meetingTitle && displayName) {
      metadata.meetingTopic = meetingTitle.innerText;

      const rosterElement = document.getElementById('roster-button');
      if (rosterElement) {
        rosterElement.click();
      } else {
        console.log('roster-button Button not found in Teams page');
      }
      chrome.runtime.sendMessage({
        action: "UpdateMetadata",
        metadata: metadata
      });
      clearInterval(intervalId); // Stop checking once the element is found
    }
  }, 2000); // Check every 2000 milliseconds (2 seconds)
}

let audioStageObserver;
let activeSpeakerObserver;

const startAudioStageObserver = () => {
  const audioStage = document.querySelector('div[data-tid="audio-stage"]');

  if (audioStage) {
    const config = { childList: true, subtree: true };
    const callback = (mutationsList, observer) => {
      mutationsList.forEach((mutation) => {
        if (mutation.type === 'childList') {
          console.log('Audio stage child elements changed');
          mutation.addedNodes.forEach(node => {
            if (node.nodeType === Node.ELEMENT_NODE) {
              // console.log('Added node:', node);
            }
          });
          mutation.removedNodes.forEach(node => {
            if (node.nodeType === Node.ELEMENT_NODE) {
              // console.log('Removed node:', node);
            }
          });
          if (activeSpeakerObserver) {
            activeSpeakerObserver.disconnect();
            startObserver();
          }
        }
      });
    };

    if (audioStageObserver) {
      audioStageObserver.disconnect();
    }
    audioStageObserver = new MutationObserver(callback);
    audioStageObserver.observe(audioStage, config);
    console.log('Audio stage observer started');
  } else {
    console.log('Audio stage not found. Retrying in 2 seconds...');
    setTimeout(startAudioStageObserver, 2000);
  }
};

// Function to start the MutationObserver
const startObserver = () => {
  // Select the node that will be observed for mutations
  const targetNodes = document.querySelectorAll('div[data-tid="participant-speaker-ring"]');

  // Options for the observer (which mutations to observe)
  const config = { attributes: true, attributeOldValue: true };

  // Callback function to execute when mutations are observed
  const callback = (mutationsList, observer) => {
    mutationsList.forEach((mutation) => {
      console.log(mutation);
      if (mutation.type === 'attributes') {
        console.log(`Attribute mutation detected: ${mutation.attributeName}`);
        const oldValue = mutation.oldValue;
        const newValue = mutation.target.getAttribute(mutation.attributeName);
        // console.log(`Old value: ${oldValue}`);
        // console.log(`New value: ${newValue}`);
        if (oldValue.startsWith(inactiveRingClass) && newValue.startsWith(activeRingClass)) {
          // console.log("Active Speaker participant-speaker-ring activated");
          let parentElement = mutation.target.parentElement.parentElement;
          if (parentElement && parentElement.hasAttribute('aria-label')) {
            const ariaLabel = parentElement.getAttribute('aria-label');
            // console.log(`Found aria-label: ${ariaLabel}`);
            if (ariaLabel.includes('muted')) {
              // console.log('The ariaLabel contains the word "muted".');
            } else {
              const activeSpeaker = ariaLabel.split(',')[0];
              console.log(`Active Speaker Change: ${activeSpeaker}`);
              chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
            }
          } else {
            console.log('No aria-label found at expected parent level.');
          }
        }
        else if (oldValue.startsWith(activeRingClass) && newValue.startsWith(inactiveRingClass)) {
          let foundActiveSpeaker = false;
          console.log("Active Speaker participant-speaker-ring stopped, findout who else is speaking");
          const speakerList = document.querySelectorAll('div[data-cid="calling-participant-stream"]');
          speakerList.forEach((speakerDiv, index) => {
            //select the first div element with attribute of data-tid="participant-speaker"
            const participantSpeaker = speakerDiv.querySelector('div[data-tid="participant-speaker"]');
            if (participantSpeaker) {
              // console.log(`Found participant speaker div for speaker ${index + 1}`);
              const participantSpeakerRing = participantSpeaker.querySelector('div[data-tid="participant-speaker-ring"]');
              if (participantSpeakerRing) {
                const ringClass = participantSpeakerRing.getAttribute('class');
                if (ringClass.startsWith(activeRingClass)) {
                  const ariaLabel = speakerDiv.getAttribute('aria-label');
                  if (ariaLabel.includes('muted')) {
                    // console.log('The ariaLabel contains the word "muted".');
                  } else {
                    const activeSpeaker = ariaLabel.split(',')[0];
                    console.log(`Active Speaker Change: ${activeSpeaker}`);
                    chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
                    foundActiveSpeaker = true;
                    return true;
                  }
                }
              }
            }
          });
          if (!foundActiveSpeaker) {
            console.log(`No more active speakers.`);
            chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: "N/A" });
          }
        }
      }
    });
  };

  if (activeSpeakerObserver) {
    activeSpeakerObserver.disconnect();
  }
  // Create an observer instance linked to the callback function
  activeSpeakerObserver = new MutationObserver(callback);

  // Start observing the target nodes for configured mutations
  if (targetNodes.length > 0) {
    targetNodes.forEach(node => {
      activeSpeakerObserver.observe(node, config);
    });
    console.log('MutationObserver started for child nodes');
  } else {
    console.log('Target node not found. Retrying in 2 seconds...');
    setTimeout(startObserver, 2000); // Retry after 2 seconds
  }
};

window.onload = function () {
  const muteObserver = new MutationObserver((mutationList) => {
    mutationList.forEach((mutation) => {
      if (mutation.type === 'attributes' && mutation.attributeName === 'aria-label') {
        const muteButton = mutation.target;
        if (muteButton.getAttribute('aria-label').includes('Unmute')) {
          chrome.runtime.sendMessage({ action: "MuteChange", mute: true });
          console.log("Mute detected");
        } else if (muteButton.getAttribute('aria-label').includes('Mute')) {
          chrome.runtime.sendMessage({ action: "MuteChange", mute: false });
          console.log("Unmute detected");
        }
      }
    });
  });

  const muteInterval = setInterval(() => {
    const muteButton = document.querySelector('[aria-label*="Mute"], [aria-label*="Unmute"]');
    if (muteButton) {
      muteObserver.observe(muteButton, { attributes: true });
      clearInterval(muteInterval);
    }
  }, 2000);

  checkForMeetingMetadata();

  startAudioStageObserver();

  startObserver();
};