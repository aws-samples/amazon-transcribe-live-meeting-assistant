/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
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