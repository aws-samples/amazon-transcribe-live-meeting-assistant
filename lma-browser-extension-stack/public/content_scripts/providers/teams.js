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
    if (message.includes('stopped')) {
      console.log("Recording stopped");
      activeSpeakerObserver.disconnect();
  } else {
      console.log("Start Listening");
      startObserver();
  }
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

let activeSpeakerObserver = null;
// Function to start the MutationObserver
const startObserver = () => {
  // Step 1: Locate the roster title element
  const rosterTitleElement = document.querySelector('span[id^="roster-title-section"][aria-label^="In this meeting"]');
  console.log('Roster Title Element:', rosterTitleElement);

  let targetDivElement = null;

  if (rosterTitleElement) {
    // Step 2: Locate the closest div with role="treeitem"
    const treeItemDiv = rosterTitleElement.closest('div[role="treeitem"]');
    console.log('Tree Item Div:', treeItemDiv);

    // Step 3: Get its parent div
    if (treeItemDiv) {
      targetDivElement = treeItemDiv.parentElement.parentElement;
      console.log('Target Div Element:', targetDivElement);
    }
  }

  // Retry if target element is not found
  if (!targetDivElement) {
    console.log('Target div not found. Retrying in 2 seconds...');
    setTimeout(startObserver, 2000); // Retry after 2 seconds
    return;
  }

  // Options for the observer (which mutations to observe)
  const config = { childList: true, subtree: true, attributes: true, attributeOldValue: true };

  // Callback function to execute when mutations are observed
  const callback = (mutationsList, observer) => {
    console.log('Callback function triggered');
    mutationsList.forEach((mutation) => {
      if (mutation.type === 'childList') {
        console.log('A speaker added or removed.');
        console.log(mutation);
      } else if (mutation.type === 'attributes') {
        console.log(`The ${mutation.attributeName} attribute was modified.`);
        console.log(mutation);
        const targetElement = mutation.target;
        const isSpan = targetElement.tagName.toLowerCase() === 'span';
        const hasRolePresentation = targetElement.getAttribute('role') === 'presentation';

        if (isSpan && hasRolePresentation) {
          console.log('Both conditions are true: the element is a span and its role attribute is presentation.');
          const oldValue = mutation.oldValue;
          const newValue = mutation.target.getAttribute(mutation.attributeName);

          if (oldValue && newValue && oldValue.startsWith(inactiveRingClass) && newValue.startsWith(activeRingClass)) {
            console.log('Active Speaker participant-speaker-ring activated');
            let parentElement = mutation.target.closest('li');
            if (parentElement && parentElement.hasAttribute('aria-label')) {
              const ariaLabel = parentElement.getAttribute('aria-label');
              if (ariaLabel && !ariaLabel.endsWith(' Muted')) {
                const activeSpeaker = ariaLabel.split(',')[0];
                console.log(`Active Speaker Change: ${activeSpeaker}`);
                chrome.runtime.sendMessage({ action: 'ActiveSpeakerChange', active_speaker: activeSpeaker });
              }
            } else {
              console.log('No aria-label found at expected parent level.');
            }
          } else if (oldValue && newValue && oldValue.startsWith(activeRingClass) && newValue.startsWith(inactiveRingClass)) {
            console.log('Active Speaker stopped, finding out who else is speaking');
            let foundActiveSpeaker = false;

            const otherActiveSpeaker = targetDivElement.querySelector(`span[role="presentation"][class^="${activeRingClass}"]`);
            if (otherActiveSpeaker) {
              const otherActiveSpeakerLi = otherActiveSpeaker.closest('li');
              if (otherActiveSpeakerLi && otherActiveSpeakerLi.hasAttribute('aria-label')) {
                const ariaLabel = otherActiveSpeakerLi.getAttribute('aria-label');
                if (ariaLabel && !ariaLabel.endsWith(' Muted')) {
                  const activeSpeaker = ariaLabel.split(',')[0];
                  foundActiveSpeaker = true;
                  console.log(`Active Speaker Change: ${activeSpeaker}`);
                  chrome.runtime.sendMessage({ action: 'ActiveSpeakerChange', active_speaker: activeSpeaker });
                }
              }
            }

            if (!foundActiveSpeaker) {
              console.log('No more active speakers.');
              chrome.runtime.sendMessage({ action: 'ActiveSpeakerChange', active_speaker: 'n/a' });
            }
          }
        }
      }
    });
  };

  // Create an observer instance linked to the callback function
  activeSpeakerObserver = new MutationObserver(callback);

  // Start observing the target nodes for configured mutations
  activeSpeakerObserver.observe(targetDivElement, config);
  console.log('MutationObserver started for active speakers');
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

};