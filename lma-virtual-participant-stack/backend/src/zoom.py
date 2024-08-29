
import asyncio
import details
import scribe
from playwright.async_api import TimeoutError


async def meeting(page):

    print("Getting meeting link.")
    await page.goto(f"https://zoom.us/wc/{details.meeting_id}/join")

    print("Typing meeting password.")
    try:
        password_text_element = await page.wait_for_selector('#input-for-pwd')
    except TimeoutError:
        print("LMA Virtual Participant was unable to join the meeting.")
        return
    else:
        await password_text_element.type(details.meeting_password)

    print("Entering name.")
    name_text_element = await page.wait_for_selector('#input-for-name')
    await name_text_element.type(details.lma_identity)
    await name_text_element.press("Enter")

    print("Adding audio.")
    try:
        audio_button_element = await page.wait_for_selector(
            "text=Join Audio by Computer",
            timeout=details.waiting_timeout
        )
    except TimeoutError:
        print("LMA Virtual Participant was not admitted into the meeting.")
        return
    else:
        await audio_button_element.click()

    print("Opening chat panel.")
    chat_button_element = await page.wait_for_selector(
        'button[aria-label^="open the chat panel"]'
    )
    await chat_button_element.hover()
    await chat_button_element.click()

    async def send_messages(messages):
        message_element = await page.wait_for_selector(
            'div[aria-placeholder="Type message here..."]'
        )
        for message in messages:
            await message_element.fill(message)
            await message_element.press('Enter')

    print("Sending introduction messages.")
    await send_messages(details.intro_messages)

    async def attendee_change(number: int):
        if number <= 1:
            print("LMA Virtual Participant got lonely and left.")
            details.start = False
            await page.goto("about:blank")

    await page.expose_function("attendeeChange", attendee_change)

    print("Listening for attendee changes.")
    await page.evaluate('''
        const targetNode = document.querySelector('.footer-button__number-counter')
        const config = { characterData: true, subtree: true }

        const callback = (mutationList, observer) => {
            attendeeChange(parseInt(mutationList[mutationList.length - 1].target.textContent))
        }

        const observer = new MutationObserver(callback)
        observer.observe(targetNode, config)
    ''')

    await page.expose_function("speakerChange", scribe.speaker_change)

    print("Listening for speaker changes.")
    await page.evaluate('''
        const targetNode = document.querySelector(
            '.speaker-active-container__video-frame .video-avatar__avatar .video-avatar__avatar-title'
        )
        const config = { childList: true, subtree: true }

        const callback = (mutationList, observer) => {
            for (const mutation of mutationList) {
                const new_speaker = mutation.target.textContent
                if (new_speaker) speakerChange(new_speaker)
            }
        }

        const observer = new MutationObserver(callback)
        observer.observe(targetNode, config)
                        
        const initial_speaker = targetNode.textContent
        if (initial_speaker) speakerChange(initial_speaker)
    ''')

    # start the transcription if details.start flag is true
    if details.start:
        print(details.start_messages[0])
        await send_messages(details.start_messages)
        asyncio.create_task(scribe.transcribe())

    async def message_change(message):
        print('New Message:', message)
        if details.end_command in message:
            print("LMA Virtual Participant has been removed from the meeting.")
            details.start = False
            await send_messages(details.exit_messages)
            await page.goto("about:blank")
        elif details.start and details.pause_command in message:
            details.start = False
            print(details.pause_messages[0])
            await send_messages(details.pause_messages)
        elif not details.start and details.start_command in message:
            details.start = True
            print(details.start_messages[0])
            await send_messages(details.start_messages)
            asyncio.create_task(scribe.transcribe())
        elif details.start:
            details.messages.append(message)

    await page.expose_function("messageChange", message_change)

    print("Listening for message changes.")
    await page.evaluate('''
        const targetNode = document.querySelector('div[aria-label="Chat Message List"]')
        const config = { childList: true, subtree: true }

        const callback = (mutationList, observer) => {
            const addedNode = mutationList[mutationList.length - 1].addedNodes[0]
            if (addedNode) {
                message = addedNode.querySelector('div[id^="chat-message-content"]')?.getAttribute('aria-label')
                if (message && !message.startsWith("You to Everyone")) {
                    messageChange(message)
                }
            }
        }

        const observer = new MutationObserver(callback)
        observer.observe(targetNode, config)
    ''')

    print("Waiting for meeting end.")
    try:
        done, pending = await asyncio.wait(
            fs=[
                asyncio.create_task(page.wait_for_selector(
                    'button[aria-label="Leave"]', state="detached", timeout=0)),
                asyncio.create_task(page.wait_for_selector(
                    'div[class="zm-modal zm-modal-legacy"]', timeout=0))
            ],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=details.meeting_timeout
        )
        [task.cancel() for task in pending]
        print("Meeting ended.")
    except:
        print("Meeting timed out.")
    finally:
        details.start = False
