console.log("Inside LMA Chime script");

let metadata = {
  baseUrl: window.location.origin
}

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


/*********** Detecting mute or unmute *************/
const handleMuteChanges = function (summaries) {
  console.log("Mute change detected");
  console.log(summaries);

  let isMuted = false;
  for (let element of document.getElementsByClassName('footer-button-base__button-label')) {
    if (element.innerText === "Unmute") {
      isMuted = true;
    }
  }
  chrome.runtime.sendMessage({action: "MuteChange", mute: isMuted});
};

const muteObserver = new MutationObserver((mutationList) => {
  console.log("mute changed");
  if (mutationList[0].target.textContent.indexOf('Unmute') >= 0) {
    chrome.runtime.sendMessage({action: "MuteChange", mute: true});
  } else {
    chrome.runtime.sendMessage({action: "MuteChange", mute: false});
  }
});

const activeSpeakerObserver = new MutationObserver((mutationList) => {
  console.log("activeSpeaker changed");
  console.log(mutationList);
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

// injectScript('content_scripts/providers/chime-injection.js');


window.onload = function() {
    
  const muteInterval = setInterval(() => {
    const muteButton = document.getElementById('audio');
    console.log('checking for mute button');
    if (muteButton) {
      console.log('mute button found');
      activeSpeakerObserver.observe(muteButton, { attributes: true, subtree: false, childList: false });
      clearInterval(muteInterval);
    }
  }, 2000);

  const titleInterval = setInterval(() => {
    console.log('Checking for title');
    const titles = document.querySelectorAll('[data-test-id="meetingTitle"]');
    if (titles.length > 0) {
      console.log('Title found');
      let title = titles[0].innerText;
      metadata.meetingTopic = title;
      chrome.runtime.sendMessage({
        action: "UpdateMetadata",
        metadata: metadata
      });
      clearInterval(titleInterval);
    }
  }, 2000);

  const activeSpeaker = setInterval(() => {
    console.log('checking for active speaker div');
    const speakers = document.getElementsByClassName('activeSpeakerCell');
    if (speakers && speakers.length > 0) {
      console.log('active speaker div found');
      activeSpeakerObserver.disconnect()
      activeSpeakerObserver.observe(speakers[0], { attributes: true, childList: true, subtree: true });
      //clearInterval(activeSpeaker);
    }
  }, 2000)

  /*
  var muteObserver = new MutationSummary({
    callback: handleMuteChanges,
    queries: [
      { element: '#audio' },
      { element: '.outlook__button'}
    ]
  });


  var speakerObserver = new MutationSummary({
    callback: handleActiveSpeakerChanges,
    queries: [
      { element: '.roster-speaker' },
      { element: '.activeSpeakerCell' },
    ]
  });


  var observer = new MutationSummary({
    callback: handleParticipantChange,
    queries: [
      { element: '.video-avatar__avatar' }
    ]
  });*/

  
};