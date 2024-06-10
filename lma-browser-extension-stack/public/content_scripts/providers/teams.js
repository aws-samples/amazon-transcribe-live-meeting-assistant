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

// function checkValues(oldValue, newValue) {

//   if (cleanNewValue === oldValue) {
//       // If they match, return the part of the oldValue before the first comma
//       return oldValue.split(',')[0].trim();
//   } else {
//       // Optional: Return some indication of no match or other action
//       return "Values do not match";
//   }
// }

// Function to start the MutationObserver
const startObserver = () => {
  // Select the node that will be observed for mutations
  const targetNodes = document.querySelectorAll('div[data-tid="participant-speaker-ring"]');

  if (targetNodes.length) {
    // Options for the observer (which mutations to observe)
    const config = { attributes: true, attributeOldValue: true };

    // Callback function to execute when mutations are observed
    const callback = (mutationsList, observer) => {
      //get 'participant-speaker-ring' mute class - to do
      for (let mutation of mutationsList) {
        console.log(mutation);
        if (mutation.type === 'attributes') {
          console.log(`Attribute mutation detected: ${mutation.attributeName}`);
          const oldValue = mutation.oldValue;
          const newValue = mutation.target.getAttribute(mutation.attributeName);
          console.log(`Old value: ${oldValue}`);
          console.log(`New value: ${newValue}`);
          // const activeSpeaker = checkValues(oldValue, newValue);
          if (oldValue.startsWith("fui-Primitive ___19upu4n") && newValue.startsWith("fui-Primitive ___s78zj80")) {
            console.log("Active Speaker participant-speaker-ring");
            let parentElement = mutation.target.parentElement.parentElement;
            if (parentElement && parentElement.hasAttribute('aria-label')) {
              const ariaLabel = parentElement.getAttribute('aria-label');
              console.log(`Found aria-label: ${ariaLabel}`);
              // 1. Check if the ariaLabel does not contain the string 'muted'
              if (ariaLabel.includes('muted')) {
                console.log('The ariaLabel contain the word "muted".');
              } else {
                // 2. Since the ariaLabel does not contains 'muted', we proceed to get the string before the first comma
                const activeSpeaker = ariaLabel.split(',')[0]; // Split the string by comma and take the first element
                console.log(`Active Speaker Change: ${activeSpeaker}`);
                chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
              }
            } else {
              console.log('No aria-label found at expected parent level.');
            }
          }

        } else if (mutation.type === 'childList') {
          console.log(`Child list mutation detected. Added nodes: ${mutation.addedNodes.length}, Removed nodes: ${mutation.removedNodes.length}`);
        }
      }
    };

    // Create an observer instance linked to the callback function
    const activeSpeakerObserver = new MutationObserver(callback);

    // Start observing the target node for configured mutations
    targetNodes.forEach(node => {
      activeSpeakerObserver.observe(node, config);
    });
    console.log('MutationObserver started');
  } else {
    console.log('Target node not found. Retrying in 2 seconds...');
    setTimeout(startObserver, 2000); // Retry after 2 seconds
  }
};

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

  startObserver();
};
