/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
const checkVariable = setInterval(() => {
  if(typeof MeetingConfig !== 'undefined') {
    console.log('meeting config defined:', MeetingConfig);
    window.postMessage({ type: "MeetingConfig", value: MeetingConfig });
  } else {
    console.log('MeetingConfig not yet defined.');
  }
  clearInterval(checkVariable);
}, 1000);