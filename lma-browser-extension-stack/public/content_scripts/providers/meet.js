console.log("Inside LMA Google Meet script");

let metadata = {
    baseUrl: window.location.origin
}
var displayName;

const openChatPanel = function ()
{
    const chatPanelButton = document.querySelector('button[aria-label="Chat with everyone"]');
    if (chatPanelButton)
    {
        chatPanelButton.click();
    }
    else
    {
        console.log("chatPanelButton not found.");
    }
}

const sendChatMessage = function (message)
{
    const chatInput = document.querySelector('textarea[aria-label="Send a message"]');
    if (chatInput)
    {
        chatInput.value = message;
        var inputEvent = new Event('input', { bubbles: true, cancelable: true });
        chatInput.dispatchEvent(inputEvent);

        setTimeout(() =>
        {
            const sendChatButton = document.querySelector('button[aria-label="Send a message"]');
            if (sendChatButton)
            {
                sendChatButton.click();
            }
            else
            {
                console.log("sendChatButton not found.");
            }
        }, 1000);
    }
    else
    {
        console.log("chatInput not found.");
    }
};

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse)
{
    if (request.action === "FetchMetadata")
    {
        checkForMeetingMetadata();
    }
    if (request.action === "SendChatMessage")
    {
        console.log("received request to send a chat message");
        console.log("message:", request.message);
        if (document.querySelector('button[aria-label="Chat with everyone"]').getAttribute("aria-pressed") != "true")
        {
            openChatPanel();
        }

        setTimeout(() =>
        {
            sendChatMessage(request.message)
        }, 2000);
    }
});

const checkForMeetingMetadata = function ()
{
    setTimeout(() =>
    {
        try
        {
            metadata.userName = document.querySelector('script[class="ds:8"]').innerText.match(/data:\[(.+?)\]/)[1].split(',')[6].replaceAll("\"", "")
            displayName = metadata.userName;
        }
        catch (error)
        {
            console.log("Failed to get username:", error);
        }

        const titleElement = document.querySelector('div[data-meeting-title]');
        if (titleElement)
        {
            metadata.meetingTopic = titleElement.getAttribute('data-meeting-title');
        }
        else
        {
            console.log("titleElement not found.");
        }

        console.log("Sending Metadata:", metadata);

        chrome.runtime.sendMessage({
            action: "UpdateMetadata",
            metadata: metadata
        });
    }, 2000);
}

window.onload = function ()
{
    // Mute Observer
    let currentMuteState = false; // to avoid duplicate messages
    const muteObserver = new MutationObserver((mutationList) =>
    {
        mutationList.forEach((mutation) =>
        {
            if (mutation.type === 'attributes' && mutation.attributeName === 'data-is-muted')
            {
                const muteButton = mutation.target;

                if (muteButton.getAttribute('data-is-muted') == "true" && currentMuteState == false)
                {
                    currentMuteState = true;
                    console.log("Mute detected");
                    chrome.runtime.sendMessage({ action: "MuteChange", mute: true });
                }
                else if (muteButton.getAttribute('data-is-muted') == "false" && currentMuteState == true)
                {
                    currentMuteState = false;
                    console.log("Unmute detected");
                    chrome.runtime.sendMessage({ action: "MuteChange", mute: false });
                }
            }
        });
    });

    const muteInterval = setInterval(() =>
    {
        const muteButton = document.querySelector('button[data-mute-button]');
        if (muteButton)
        {
            muteObserver.observe(muteButton, { attributes: true, attributeFilter: ['data-is-muted'], subtree: false, childList: false });
            clearInterval(muteInterval);
            console.log('Mute observer started');
        }
        else
        {
            console.log('muteButton not found. Retrying in 2 seconds...');
        }
    }, 2000);

    // Active Speaker Observer
    let lastActiveSpeaker = ''; // to avoid duplicate messages
    const activeSpeakerObserver = new MutationObserver((mutationsList, observer) =>
    {
        mutationsList.forEach((mutation) =>
        {
            if (mutation.type === "attributes")
            {
                const targetElement = mutation.target;
                if (targetElement.getAttribute('jscontroller') == "ES310d" && mutation.attributeName === 'class')
                {
                    if (targetElement.offsetParent !== null) // Check if the indicator element is visible
                    {
                        const container = targetElement.closest("div[data-participant-id]");
                        const activeSpeaker = container.querySelector('[data-tooltip-id][data-tooltip-anchor-boundary-type] span.notranslate').innerText;

                        if (activeSpeaker !== lastActiveSpeaker)
                        {
                            lastActiveSpeaker = activeSpeaker;
                            console.log(`Active Speaker Changed: ${activeSpeaker}`);
                            chrome.runtime.sendMessage({ action: "ActiveSpeakerChange", active_speaker: activeSpeaker });
                        }
                    }
                }
            }
        });
    });

    const activeSpeakerInterval = setInterval(() =>
    {
        const speakerPanel = document.querySelector('div[data-participant-id]')?.parentElement?.parentElement;
        if (speakerPanel)
        {
            if (speakerPanel.hasAttribute('LMAAttached')) 
            {
                clearInterval(activeSpeakerInterval);
                return;
            }

            speakerPanel.setAttribute('LMAAttached', 'true');
            console.log(speakerPanel)

            activeSpeakerObserver.observe(speakerPanel, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });

            clearInterval(activeSpeakerInterval);
            console.log('activeSpeaker observer started');
        }
        else
        {
            console.log('speakerPanel not found. Retrying in 2 seconds...');
        }
    }, 2000);

    checkForMeetingMetadata();
};