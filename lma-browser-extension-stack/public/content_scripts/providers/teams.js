console.log("Inside LMA Teams script");

let metadata = {
  baseUrl: window.location.origin
}
var displayName;

const openChatPanel = function () {
  const chatPanelButton = document.getElementById('chat-button');
  // click chatPanelButton if found
  if (chatPanelButton) {
    chatPanelButton.click();
  }
}

const activeRingClass = "fui-Avatar r81b29z ___l339ri0";
const inactiveRingClass = "fui-Avatar r81b29z ___1okzwt8";

const sendChatMessage = function (message) {
  /* const findChatInputAndSend = function () {
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
  } */
};

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "FetchMetadata") {
    checkForMeetingMetadata();
  }
  if (request.action === "SendChatMessage") {
    console.log("received request to send a chat message");
    console.log("message:", request.message);
    /* let chatInput = document.querySelector('p[data-placeholder="Type a message"]');
    if (!chatInput) {
      openChatPanel();
    } 
    sendChatMessage(request.message);
    */
    startObserver();
  }
});

const checkForMeetingMetadata = function () {
  setTimeout(() => {
    if (!metadata.userName || metadata.userName.trim() === '') {
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
    if (!metadata.meetingTopic || metadata.meetingTopic.trim() === '') {
      /* const showMoreButton = document.getElementById('callingButtons-showMoreBtn');
      if (showMoreButton) {
        showMoreButton.click();
      }
      const meetingInfoButton = document.querySelector('[aria-label="Meeting info"]');
      if (meetingInfoButton) {
        meetingInfoButton.click();
      } */
      //const meetingTitle = document.querySelector('[data-tid="call-title"]');
      const meetingTitle = document.title;
      if (meetingTitle && displayName) {
        metadata.meetingTopic = meetingTitle;
        //setInterval(checkAndClickRoster, 2000);
      }
    }
    if (metadata.userName && metadata.userName.trim() !== '' && metadata.meetingTopic && metadata.meetingTopic.trim() !== '') {
      chrome.runtime.sendMessage({
        action: "UpdateMetadata",
        metadata: metadata
      });
    }
  }, 2000);
}

let activeSpeakerObserver;
/* let audioStageObserver;

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
  const targetNodes = document.querySelectorAll('span[role="presentation"][id^="avatar-"]');

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
          let parentElement = mutation.target.parentElement.parentElement.parentElement;
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
            chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: "n/a" });
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
}; */


// Function to start the MutationObserver
const startObserver = () => {
  //data-cid="calling-participant-stream"
  let targetNodes = document.querySelector('div[aria-label="Shared content view"][role=main]');
  if (targetNodes && targetNodes.hasAttribute('LMAAttached') && targetNodes.getAttribute('LMAAttached') === 'true') {
    return;
  }
  else if (!targetNodes){
    console.log('Target div not found. Retrying in 2 seconds...');
    setTimeout(startObserver, 2000);
    return;
  }
  else{
    targetNodes.setAttribute('LMAAttached', 'true');
  }
  console.log(targetNodes)
  // Options for the observer (which mutations to observe)
  const config = { childList: true, subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ['class'] };

  // Callback function to execute when mutations are observed
  const callback = (mutationsList, observer) => {
    mutationsList.forEach((mutation) => {
      if (mutation.type === "childList") {
        mutation.addedNodes.forEach(node => {
          if (node.nodeName === 'LI' && node.getAttribute('role') === 'presentation') {
            console.log("A speaker added.");
            console.log(node);
            activeSpeakerObserver.disconnect();
            startObserver();
          }
        });
        mutation.removedNodes.forEach(node => {
          if (node.nodeName === 'LI' && node.getAttribute('role') === 'presentation') {
            console.log("A speaker removed.");
            console.log(node);
            activeSpeakerObserver.disconnect();
            startObserver();
          }
        });
      } else if (mutation.type === "attributes") {
        console.log(`The ${mutation.attributeName} attribute was modified.`);
        console.log(mutation);
        const targetElement = mutation.target;
        const isDiv = targetElement.tagName.toLowerCase() === 'div';
        const hasRolePresentation = targetElement.getAttribute('data-tid') === 'voice-level-stream-outline';
        //check if the mutitation is on attribute class
        if (isDiv && hasRolePresentation && mutation.attributeName === 'class') {
          console.log('Both conditions are true: the element is a div and its data-cid attribute is voice-level-stream-outline.');
          const oldValue = mutation.oldValue;
          const newValue = mutation.target.getAttribute(mutation.attributeName);
          if (newValue.includes('vdi-frame-occlusion') && !oldValue.includes('vdi-frame-occlusion')) {
            // console.log("Active Speaker participant-speaker-ring activated");
            let parentElement = mutation.target.parentElement;
            if (parentElement && parentElement.hasAttribute('aria-label')) {
              const ariaLabel = parentElement.getAttribute('aria-label');
              // console.log(`Found aria-label: ${ariaLabel}`);
              if (ariaLabel && ariaLabel.endsWith(' Muted')) {
                // console.log('The ariaLabel contains the word "Muted".');
              } else {
                const activeSpeaker = ariaLabel.split(',')[0];
                console.log(`Active Speaker Change: ${activeSpeaker}`);
                chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
              }
            } else {
              console.log('No aria-label found at expected parent level.');
            }
          }
          else if (!newValue.includes('vdi-frame-occlusion') && oldValue.includes('vdi-frame-occlusion')) {
            let foundActiveSpeaker = false;
            console.log("Active Speaker stopped, findout who else is speaking");
            //query the active speaker:
            const otherActiveSpeaker = targetNodes.querySelector(`div[class*="vdi-frame-occlusion"]`)
            if (otherActiveSpeaker) {
              const otherActiveSpeakerLi = otherActiveSpeaker.parentElement;
              if (otherActiveSpeakerLi && otherActiveSpeakerLi.hasAttribute('aria-label')) {
                const ariaLabel = otherActiveSpeakerLi.getAttribute('aria-label');
                // console.log(`Found aria-label: ${ariaLabel}`);
                if (ariaLabel && ariaLabel.endsWith(' Muted')) {
                  // console.log('The ariaLabel contains the word "Muted".');
                } else {
                  const activeSpeaker = ariaLabel.split(',')[0];
                  foundActiveSpeaker = true;
                  console.log(`Active Speaker Change: ${activeSpeaker}`);
                  chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
                }
              }
            }
            if (!foundActiveSpeaker) {
              console.log(`No more active speakers.`);
              chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: "n/a" });
            }
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
  if (targetNodes) {
    activeSpeakerObserver.observe(targetNodes, config);
    console.log('MutationObserver started active speakers');
  } else {
    console.log('Target node not found. Retrying in 2 seconds...');
    setTimeout(startObserver, 2000); // Retry after 2 seconds
  }
};

function checkAndClickRoster() {
  // const rosterElement = document.getElementById('roster-button');
  // if (rosterElement && rosterElement.getAttribute('data-track-action-outcome') === 'show') {
  //   rosterElement.click();
  // }
}

function checkAndStartObserver() {
  const rosterTitleElement = document.querySelector('span[id^="roster-title-section"][aria-label^="In this meeting"]');
  if (rosterTitleElement) {
    let targetNodes  = rosterTitleElement.parentElement.parentElement.parentElement.parentElement.parentElement;
    if (targetNodes && targetNodes.hasAttribute('LMAAttached') && targetNodes.getAttribute('LMAAttached') === 'true') {
    } else {
      console.log('LMAAttached is not attached, startObserver()');
      startObserver();
    }
  }
}

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
  // startObserver();
  // setInterval(checkAndStartObserver, 5000);
};