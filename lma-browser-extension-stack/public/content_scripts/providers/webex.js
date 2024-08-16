console.log("Inside LMA Teams script");

let metadata = {
  baseUrl: window.location.origin
}
var displayName;

const openChatPanel = function () {
  const chatPanelButton = document.querySelector('button[data-test="in-meeting-chat-toggle-button"]');
  if (chatPanelButton) {
    chatPanelButton.click();
  }
}


const sendChatMessage = function (message) {
  const composerDiv = document.querySelector('div[class="composer"]');
  if (composerDiv) {
    const chatInput = composerDiv.querySelector('div#quill-composer div.ql-editor');
    if (chatInput) {
      chatInput.innerHTML = `<p>${message}</p>`;
      var inputEvent = new Event('input', { bubbles: true, cancelable: true });
      chatInput.dispatchEvent(inputEvent);
      setTimeout(() => {
        const sendChatButton = composerDiv.querySelector('button[aria-label="Send message"]');
        if (sendChatButton) {
          sendChatButton.click();
        } else {
          console.log("sendChatButton not found.");
        }
      }, 1000);
    }
    else {
      console.log("chat input div not found.");
    }
  }
  else {
    console.log("composer div not found.");
  }
};

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "FetchMetadata") {
    checkForMeetingMetadata();
  }
  if (request.action === "SendChatMessage") {
    console.log("received request to send a chat message");
    console.log("message:", request.message);
    let chatInput = document.querySelector('div[id="quill-composer"]');
    if (!chatInput) {
      openChatPanel();
    }
    setTimeout(sendChatMessage(request.message), 2000);
    setTimeout(function() {
      setInterval(startObserver, 10000);
  }, 2000);
  }
});

const checkForMeetingMetadata = function () {
  setTimeout(() => {
    if (!metadata.userName || metadata.userName.trim() === '') {
      //get the user
      const crossLaunchDataScript = document.querySelector('#crossLaunchData');
      if (crossLaunchDataScript) {
        console.log(crossLaunchDataScript.textContent);
      } else {
        console.log("crossLaunchData script not found");
      }
      if (crossLaunchDataScript) {
        try {
          const crossLaunchData = JSON.parse(crossLaunchDataScript.textContent);
          if (crossLaunchData.currentUser && crossLaunchData.currentUser.displayName) {
            metadata.userName = crossLaunchData.currentUser.displayName;
          }
        } catch (error) {
          console.log("Unable to parse crossLaunchData to get the user name", error);
        }
      }
      else {
        let sessionData = undefined;
        try {
          sessionData = JSON.parse(localStorage.getItem("webex.user"));
          if (sessionData !== undefined && sessionData.name) {
            metadata.userName = sessionData.name;
          }
        } catch (error) {
          console.log("Unable to read webex session data", error);
        }
      }
    }
    if (!metadata.meetingTopic || metadata.meetingTopic.trim() === '') {
      const iframe = document.querySelector('iframe[id="unified-webclient-iframe"]');
      if (iframe) {
        console.log("Iframe found");
      } else {
        console.log("Iframe not found");
      }
      if (iframe) {
        try {
          const iframeDocument = iframe.contentDocument || iframe.contentWindow.document;
          const titleElement = iframeDocument.querySelector('h1[data-type="body-secondary"]');
          if (titleElement) {
            metadata.meetingTopic = titleElement.textContent.trim();
          }
        } catch (error) {
          console.log("Unable to access iframe with id=unified-webclient-iframe content", error);
        }
      }
      else {
        try {
          const titleElement = document.querySelector('h1[data-type="body-secondary"]');
          if (titleElement) {
            metadata.meetingTopic = titleElement.textContent.trim();
          }
        } catch (error) {
          console.log("Unable to access iframe with id=unified-webclient-iframe content", error);
        }
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

// Function to start the MutationObserver
const startObserver = () => {
  let targetNodes = document.querySelector('main#main-content');
  if (targetNodes && targetNodes.hasAttribute('LMAAttached') && targetNodes.getAttribute('LMAAttached') === 'true') {
    return;
  }
  else if (!targetNodes) {
    console.log('Target div not found. Retrying in 5 seconds...');
    return;
  }
  else {
    targetNodes.setAttribute('LMAAttached', 'true');
  }
  console.log(targetNodes)
  // Options for the observer (which mutations to observe)
  const config = {subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ['aria-label'] };

  // Callback function to execute when mutations are observed: div aria-label starts with Unmuted
  const callback = (mutationsList, observer) => {
    mutationsList.forEach((mutation) => {
      if (mutation.type === "childList") {
        //console.log(mutation);
      }
      else if (mutation.type === "attributes"){
        // console.log(`The ${mutation.attributeName} attribute was modified.`);
        // console.log(mutation);
        const targetElement = mutation.target;
        const isDiv = targetElement.tagName.toLowerCase() === 'div';
        if (isDiv && mutation.attributeName === 'aria-label') {
          const oldValue = mutation.oldValue;
          const newValue = mutation.target.getAttribute(mutation.attributeName);
          if (newValue.startsWith('Unmuted') && !oldValue.includes('Unmuted')) {
            const ariaLabel = targetElement.getAttribute('data-test');
            const activeSpeaker = ariaLabel.replace('-participant-label', '');
            console.log(`Active Speaker Change: ${activeSpeaker}`);
            chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
          }
          else if (oldValue.startsWith('Unmuted')){
            //find other active speaker
            const nextActiveSpeaker = document.querySelector('div[aria-label^="Unmuted"]');
            if (nextActiveSpeaker) {
              const ariaLabel = nextActiveSpeaker.getAttribute('data-test');
              const activeSpeaker = ariaLabel.replace('-participant-label', '');
              console.log(`Active Speaker Change: ${activeSpeaker}`);
              chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
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
    let targetNodes = rosterTitleElement.parentElement.parentElement.parentElement.parentElement.parentElement;
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
        if (muteButton.getAttribute('aria-label').includes('currently muted')) {
          chrome.runtime.sendMessage({ action: "MuteChange", mute: true });
          console.log("Mute detected");
        } else if (muteButton.getAttribute('aria-label').includes('currently unmuted')) {
          chrome.runtime.sendMessage({ action: "MuteChange", mute: false });
          console.log("Unmute detected");
        }
      }
    });
  });

  const muteInterval = setInterval(() => {
    const muteButton = document.querySelector('button[data-test="microphone-button"]');
    if (muteButton) {
      muteObserver.observe(muteButton, { attributes: true });
      clearInterval(muteInterval);
    }
    else {
      const iframe = document.querySelector('iframe[id="unified-webclient-iframe"]');
      if (iframe) {
        try {
          const iframeDocument = iframe.contentDocument || iframe.contentWindow.document;
          const muteButton = iframeDocument.querySelector('button[data-test="microphone-button"]');
          if (muteButton) {
            muteObserver.observe(muteButton, { attributes: true });
            clearInterval(muteInterval);
          }
        } catch (error) {
          console.log("Unable to access iframe with id=unified-webclient-iframe content", error);
        }
      }
    }
  }, 20000);

  checkForMeetingMetadata();
};