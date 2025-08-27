/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
const checkVariable = setInterval(() =>
{
    const titleElement = document.querySelector('div[data-meeting-title]');
    if (titleElement)
    {
        const titleText = titleElement.getAttribute('data-meeting-title');
        console.log('Title Found:', titleText);

        window.postMessage({ type: "Title", value: titleText });
        clearInterval(checkVariable);
    }
    else
    {
        console.log('Title not yet defined');
    }
}, 1000);