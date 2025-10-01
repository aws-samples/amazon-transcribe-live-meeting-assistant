/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
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