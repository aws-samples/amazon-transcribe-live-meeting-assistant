const checkVariable = setInterval(() => {
  const meetingTitle = document.querySelector('[data-tid="call-title"]');
  if (meetingTitle) {
    console.log('Meeting title defined:', meetingTitle.innerText);
    window.postMessage({ type: "MeetingConfig", value: { meetingTitle: meetingTitle.innerText } });
    clearInterval(checkVariable);
  } else {
    console.log('Meeting title not yet defined.');
  }
}, 1000);
