/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
 * SPDX-License-Identifier: MIT-0
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