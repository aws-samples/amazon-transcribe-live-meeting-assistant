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

//commit
const activeRingClass = "fui-Primitive ___19upu4n";
const inactiveRingClass = "fui-Primitive ___s78zj80";

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
  setTimeout(() => {
    if (!metadata.userName || metadata.userName.trim() === '') {
      try {
        sessionData = JSON.parse(localStorage.getItem("msal.activeUserProfile"));
        // console.log(sessionData);
        if (sessionData !== undefined && sessionData.name) {
          displayName = metadata.userName;
          metadata.userName = sessionData.name;
        }
      } catch (error) {
        console.log("Unable to read team live session data", error);
      }
    }
    if (!metadata.meetingTopic || metadata.meetingTopic.trim() === '') {
      // Select the span element based on type, role, and data-tid attributes
      let spanElement = document.querySelector('span[role="timer"][data-tid="call-duration"]');
      // Initialize meetingTitle variable
      let meetingTitle;
      // Check if the span element exists before extracting aria-label
      if (spanElement) {
        // Get the aria-label attribute value
        const ariaLabel = spanElement.getAttribute('aria-label');
        // Extract the meeting title from aria-label (assuming it comes before the first comma)
        meetingTitle = ariaLabel.split(',')[0].trim();
        console.log(meetingTitle); // Output the extracted meeting title
        if (meetingTitle && meetingTitle.trim()!== '') {
          metadata.meetingTopic = meetingTitle;
        }
      } else {
        console.log('Span element not found or does not match criteria, try iframe');
        const iframe = document.querySelector('iframe[id^="experience-container"]');
      if (iframe) {
        const iframeDocument = iframe.contentWindow.document;
        spanElement = iframeDocument.querySelector('span[role="timer"][data-tid="call-duration"]');
        const ariaLabel = spanElement.getAttribute('aria-label');
        // Extract the meeting title from aria-label (assuming it comes before the first comma)
        meetingTitle = ariaLabel.split(',')[0].trim();
        console.log(meetingTitle); // Output the extracted meeting title
        if (meetingTitle && meetingTitle.trim()!== '') {
          metadata.meetingTopic = meetingTitle;
        }}
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

let activeSpeakerObserver = null;
// Function to start the MutationObserver
const startObserver = () => {
  const iframe = document.querySelector('iframe[id^="experience-container"]');
  if (iframe) {
    const iframeDocument = iframe.contentWindow.document;
    // Step 1: Locate the roster title element
    // const rosterTitleElement = document.querySelector('span[id^="roster-title-section"][aria-label^="In this meeting"]');
    /* const rosterTitleElement = iframeDocument.querySelector('span[aria-label^="In this meeting"]');
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
        if (targetDivElement && targetDivElement.hasAttribute('LMAAttached') && targetDivElement.getAttribute('LMAAttached') === 'true') {
          return;
        }
        else {
          targetDivElement.setAttribute('LMAAttached', 'true');
        }
      }
    }

     */
  let targetDivElement = iframeDocument.querySelector('div[aria-label="Shared content view"][role=main]');
  if (targetDivElement && targetDivElement.hasAttribute('LMAAttached') && targetDivElement.getAttribute('LMAAttached') === 'true') {
    return;
  }
  else if (!targetDivElement){
    console.log('Target div not found. Retrying in 2 seconds...');
    setTimeout(startObserver, 2000);
    return;
  }
  else{
    targetDivElement.setAttribute('LMAAttached', 'true');
  }
  console.log(targetDivElement)


    // Options for the observer (which mutations to observe)
    const config = { subtree: true, attributes: true, attributeOldValue: true };

    // Callback function to execute when mutations are observed
    const callback = (mutationsList, observer) => {
      console.log('Callback function triggered');
      mutationsList.forEach((mutation) => {
        if (mutation.type === 'childList') {
          console.log('A speaker added or removed.');
          console.log(mutation);
        } else if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
          console.log(`The ${mutation.attributeName} attribute was modified.`);
          console.log(mutation);
          const targetElement = mutation.target;
          const isDiv = targetElement.tagName.toLowerCase() === 'div';
          let hasSpeakerRing = null;
          if (targetElement.hasAttribute('data-tid') && targetElement.getAttribute('data-tid') === 'participant-speaker-ring') {
            console.log(`'data-tid=participant-speaker-ring' attribute found on target element.`);
            hasSpeakerRing = true;
          }
          if (isDiv && hasSpeakerRing) {
            console.log('Both conditions are true: the element is a div and its data-tid attribute is participant-speaker-ring.'); 
            const oldValue = mutation.oldValue;
            const newValue = mutation.target.getAttribute(mutation.attributeName);
            //log both oldValue and newValue
            // console.log(`Old Value: ${oldValue}`);
            // console.log(`New Value: ${newValue}`);
            if (oldValue && newValue && oldValue.startsWith(inactiveRingClass) && newValue.startsWith(activeRingClass)) {
              console.log('Active Speaker participant-speaker-ring activated');
              let parentElement = mutation.target.parentElement.parentElement;
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
              const otherActiveSpeaker = targetDivElement.querySelector(`div[data-tid="participant-speaker-ring"][class^="${activeRingClass}"]`);
              if (otherActiveSpeaker) {
                const otherActiveSpeakerLi = otherActiveSpeaker.parentElement.parentElement;
                if (otherActiveSpeakerLi && otherActiveSpeakerLi.hasAttribute('aria-label') && otherActiveSpeakerLi.hasAttribute('data-cid') && otherActiveSpeakerLi.getAttribute('data-cid') === 'calling-participant-stream') {
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
  }
  else {
    console.log('Iframe not found');
    openRoster();
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
  // startRosterInterval();
  // setInterval(checkAndStartObserver, 5000);
};

function checkAndStartObserver() {
  const iframe = document.querySelector('iframe[id^="experience-container"]');
  if (iframe) {
    const iframeDocument = iframe.contentWindow.document;
    const rosterTitleElement = iframeDocument.querySelector('span[aria-label^="In this meeting"]');
    let targetDivElement = null;

    if (rosterTitleElement) {
      // Step 2: Locate the closest div with role="treeitem"
      const treeItemDiv = rosterTitleElement.closest('div[role="treeitem"]');
      //console.log('Tree Item Div:', treeItemDiv);

      // Step 3: Get its parent div
      if (treeItemDiv) {
        targetDivElement = treeItemDiv.parentElement.parentElement;
        //console.log('Target Div Element:', targetDivElement);
        if (targetDivElement && targetDivElement.hasAttribute('LMAAttached') && targetDivElement.getAttribute('LMAAttached') === 'true') {
          return;
        }
        else {
          console.log('LMAAttached is not attached, startObserver()');
          startObserver();
        }
      }
    }
  }
}

function startRosterInterval() {
  openRoster();
  setInterval(openRoster, 2000);
}

const openRoster = function () {
  const iframe = document.querySelector('iframe[id^="experience-container"]');
  if (iframe) {
    const iframeDocument = iframe.contentWindow.document;
    const rosterTitleElement = iframeDocument.querySelector('span[aria-label^="In this meeting"]');
    if (rosterTitleElement) {
      return;
    } else {
      let attempts = 0;
      const maxAttempts = 50;
      const tryOpenRoster = function () {
        const rosterElement = document.getElementById('roster-button');
        if (rosterElement) {
          rosterElement.click();
          console.log('Roster button found and clicked');
          return;
        }
        console.log('roster-button not found, trying find in iframe');
        if (iframe) {
          const iframeDocument = iframe.contentWindow.document;
          const alternativeRosterButton = iframeDocument.getElementById('roster-button');
          if (alternativeRosterButton) {
            alternativeRosterButton.click();
            console.log('Alternative roster button found from iframe and clicked');
            return;
          }
        }
        attempts++;
        if (attempts < maxAttempts) {
          console.log(`Roster button not found. Retrying in 2 seconds... (Attempt ${attempts}/${maxAttempts})`);
          setTimeout(tryOpenRoster, 2000);
        } else {
          console.log('Max attempts reached. Unable to find and click roster button.');
        }
      };
      tryOpenRoster();
    }
  }
};