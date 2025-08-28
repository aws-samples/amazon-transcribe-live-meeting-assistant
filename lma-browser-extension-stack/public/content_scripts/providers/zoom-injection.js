/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
 * SPDX-License-Identifier: MIT-0
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