console.log("Inside LMA Chime script");

let metadata = {
  baseUrl: window.location.origin
}

window.onload = function () {

  const muteObserver = new MutationObserver((mutationList) => {
    console.log("mute changed");
    if (mutationList[0].target.textContent.indexOf('Unmute') >= 0) {
      chrome.runtime.sendMessage({action: "MuteChange", mute: true});
    } else {
      chrome.runtime.sendMessage({action: "MuteChange", mute: false});
    }
  });
  
  const muteInterval = setInterval(() => {
    const muteButton = document.getElementById('audio');
    console.log('checking for mute button');
    if (muteButton) {
      console.log('mute button found');
      // muteObserver.disconnect();
      muteObserver.observe(muteButton, { attributes: true, subtree: false, childList: false });
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
  

  const activeSpeakerObserver = new MutationObserver((mutationList) => {
    console.log("activeSpeaker changed");
    console.log(mutationList);
  
    mutationList.forEach((mutation) => {
      if (mutation.addedNodes && mutation.addedNodes.length > 0) {
        const activeSpeaker = mutation.addedNodes[0].parentNode.parentNode.childNodes[1].innerText;
        console.log("Speaker:", activeSpeaker);
        if (activeSpeaker !== 'No one') {
          chrome.runtime.sendMessage({action: "ActiveSpeakerChange", active_speaker: activeSpeaker});
        }
      }
    });
  });

  const activeSpeaker = setInterval(() => {
    console.log('checking for active speaker div');
    const speakers = document.getElementsByClassName('activeSpeakerCell');
    if (speakers && speakers.length > 0) {
      console.log('active speaker div found');
      // activeSpeakerObserver.disconnect();
      activeSpeakerObserver.observe(speakers[0], { attributes: true, childList: true, subtree: true });
      clearInterval(activeSpeaker);
    }
  }, 2000);
};