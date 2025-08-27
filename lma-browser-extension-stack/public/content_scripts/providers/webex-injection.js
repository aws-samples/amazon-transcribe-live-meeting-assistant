/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
const checkVariable = setInterval(() => {
  const titles = document.querySelectorAll('[data-test-id="meetingTitle"]');
  if (titles.length > 0) {
    console.log('found title');
    window.postMessage({ type: "Title", value: titles[0].innerHTML });
    clearInterval(checkVariable);
  } else {
    console.log('title not yet defined');
  }
}, 1000);