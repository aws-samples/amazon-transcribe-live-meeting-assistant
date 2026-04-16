/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
const iframe = document.createElement("iframe");
iframe.setAttribute("hidden", "hidden");
iframe.setAttribute("id", "permissionsIFrame");
iframe.setAttribute("allow", "microphone; camera; display-capture;");
iframe.src = chrome.runtime.getURL("content_scripts/recorder/recorder.html");
document.body.appendChild(iframe);
console.log("-----LMA-------injected iframe");