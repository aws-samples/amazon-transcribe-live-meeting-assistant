console.log("Inside LMA Chime script");

let metadata = {
  baseUrl: window.location.origin
}

const openChatPanel = function () {
    const chatPanelButtons = document.querySelectorAll('[aria-label*="Open chat panel"]');
    if (chatPanelButtons.length > 0) {
      chatPanelButtons[0].click(); // open the attendee panel
    }
}

const sendChatMessage = function (message) {
  /*const titles = document.querySelectorAll('[data-testid="meetingChatInput"] textarea');
  if (titles.length > 0) {
    titles[0].value = message;
    titles[0].dispatchEvent(new Event('input', { bubbles: true }));
  }*/

  const chatPanelButtons = document.querySelectorAll('[aria-label*="Send message"]');
  if (chatPanelButtons.length > 0) {
    //document.querySelectorAll('[data-testid="meetingChatInput"] textarea')[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'H', bubbles: true }));
    var input = document.querySelectorAll('[data-testid="meetingChatInput"] textarea')[0];
    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    nativeInputValueSetter.call(input, message);

    var inputEvent = new Event('input', { bubbles: true});
    input.dispatchEvent(inputEvent);
    
    chatPanelButtons[0].click();
  }
}

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "FetchMetadata") {
    checkForMeetingMetadata();
  }
  if (request.action === "SendChatMessage") {
    console.log("received request to send a chat message");
    console.log("message:", request.message);
    let titles = document.querySelectorAll('[data-testid="meetingChatInput"] textarea');
    if (titles.length === 0) {
      openChatPanel();
    }
    sendChatMessage(request.message);
  }
});

const checkForMeetingMetadata = function() {
  setTimeout(() => {
    //console.log('Checking for title');
    let sessionData = undefined;
    try {
      sessionData = JSON.parse(JSON.parse(localStorage.getItem("AmazonChimeExpressSession")));
      if (sessionData !== undefined && sessionData.fullName) {
        metadata.userName = sessionData.fullName;
      }
    } catch (error) {
      console.log("Unable to read chime session data", error);
    }
    
    const titles = document.querySelectorAll('[data-test-id="meetingTitle"]');
    if (titles.length > 0) {
      //console.log('Title found');
      let title = titles[0].innerText;
      metadata.meetingTopic = title;
    } else {
      const title = document.title.replace("Amazon Chime: ", "");
      metadata.meetingTopic = title;
    }

    chrome.runtime.sendMessage({
      action: "UpdateMetadata",
      metadata: metadata
    });
  }, 2000);
}


window.onload = function () {

  const muteObserver = new MutationObserver((mutationList) => {
    if (mutationList[0].target.textContent.indexOf('Unmute') >= 0) {
      chrome.runtime.sendMessage({ action: "MuteChange", mute: true });
      console.log("Mute detected");
    } else {
      chrome.runtime.sendMessage({action: "MuteChange", mute: false});
      console.log("Unmute detected");
    }
  });
  
  const muteInterval = setInterval(() => {
    const muteButton = document.getElementById('audio');
    //console.log('checking for mute button');
    if (muteButton) {
      //console.log('mute button found');
      muteObserver.observe(muteButton, { attributes: true, subtree: false, childList: false });
    }
  }, 2000);

  checkForMeetingMetadata();  

  const activeSpeakerObserver = new MutationObserver((mutationList) => {
    console.log("activeSpeaker changed");
    console.log(mutationList);  
    mutationList.forEach((mutation) => {
      /*if (mutation.addedNodes && mutation.addedNodes.length > 0) {
        try {
          const activeSpeaker = mutation.addedNodes[0].parentNode.parentNode.childNodes[1].innerText;
          console.log("Speaker:", activeSpeaker);
          if (activeSpeaker !== 'No one') {
            chrome.runtime.sendMessage({action: "ActiveSpeakerChange", active_speaker: activeSpeaker});
          }
        } catch (error) {
          console.log('error detecting speaker', error);
        }
      } else */
      if (mutation.type === "characterData") {
        // this is a changed record
        // The following will ignore text that includes the word 'Mute', 'Unmute my microphone', 
        // and 'Only they may' which covers both 'Only they may mute themselves' and 'Only they may unmute themselves', which appear as text within the active speaker.
        if (!mutation.target.data.includes("Mute") && !mutation.target.data.includes("Unmute my microphone") && !mutation.target.data.includes("Only they may")) {
          const activeSpeaker = mutation.target.data;
          if (activeSpeaker !== 'No one') {
            chrome.runtime.sendMessage({action: "ActiveSpeakerChange", active_speaker: activeSpeaker});
          }
        }
        /*if (mutation.target.parentNode.parentNode.parentNode.parentNode.parentNode.parentNode.parentNode) {
          
        }
        const activeSpeaker = mutation.target.parentNode.parentNode.childNodes[1].innerText;
        console.log("CHanged innerText:", activeSpeaker);
        console.log("Changed:", mutation.target.data);*/
      }
    });
  });

  const activeSpeaker = setInterval(() => {
    //console.log('checking for active speaker div');
    const speakers = document.getElementsByClassName('activeSpeakerCell');
    if (speakers && speakers.length > 0) {
      //console.log('active speaker div found');

      if (!speakers[0].parentElement.hasAttribute("LMAAttached")) {
        console.log("UL does not have LMA attached yet, attaching.");
        activeSpeakerObserver.observe(speakers[0].parentElement, {childList: true, subtree: true, characterData:true });
        speakers[0].parentElement.setAttribute("LMAAttached", "True");
      }
            // clearInterval(activeSpeaker);
    } else {
      console.log("Speaker panel not seen. Opening up.")
      // we do not see any active speaker box. Find the open attendee button:
      const chatPanelButtons = document.querySelectorAll('[aria-label*="Open attendees"]');
      if (chatPanelButtons.length > 0) {
        chatPanelButtons[0].click(); // open the attendee panel
      }
    }
  }, 2000);
};