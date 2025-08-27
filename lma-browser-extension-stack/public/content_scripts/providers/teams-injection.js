/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
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
