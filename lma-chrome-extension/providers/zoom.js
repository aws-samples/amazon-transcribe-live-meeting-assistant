console.log("inside zoom script");

/*********** Messaging **************/
// chrome.runtime.onMessage.addListener(function(request, sender, sendResponse) {
//   console.log("Received toggle to start or stop messaging", request);
//   speakerSearchEnabled = request.toggle_speaker;
//   sendResponse({});
// });

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

var observer = new MutationSummary({
  callback: handleParticipantChange,
  queries: [
    { element: '.video-avatar__avatar' }
  ]
});

/************ This is for detecting active speaker **************/
const handleActiveSpeakerChanges = function (summaries) {
  console.log("Participant change detected");
  console.log(summaries);
  summaries.forEach(function (summary) {
    summary.added.forEach(function (newEl) {
      const speakerName = getNameForVideoAvatar(newEl);
      console.log("Active Speaker changed:", speakerName);
      chrome.runtime.sendMessage({action: "ActiveSpeakerChange", active_speaker: speakerName});
    });
  });
}

var observer = new MutationSummary({
  callback: handleActiveSpeakerChanges,
  queries: [
    { element: '.speaker-active-container__video-frame' },
    { element: '.speaker-bar-container__video-frame--active'},
    { element: '.gallery-video-container__video-frame--active'},
  ]
});

/*
const meetingApp = document.getElementById("root");
meetingApp.addEventListener("DOMNodeInserted", function(e) {
  console.log(e.target);
}, false);*/