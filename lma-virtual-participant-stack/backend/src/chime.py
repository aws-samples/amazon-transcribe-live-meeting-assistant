# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#

import asyncio
import details
import scribe
from playwright.async_api import TimeoutError
from datetime import datetime
import time


async def meeting(page):

    print("Getting meeting link.")
    await page.goto(f"https://app.chime.aws/meetings/{details.meeting_id}")

    print("Entering name.")
    try:
        name_text_element = await page.wait_for_selector('#name')
    except TimeoutError:
        print("LMA Virtual Participant was unable to join the meeting.")
        return
    else:
        await name_text_element.type(details.lma_identity)
        await name_text_element.press('Tab')
        await page.keyboard.press('Enter')

    print("Clicking mute button.")
    mute_checkbox_element = await page.wait_for_selector('text="Join muted"')
    await mute_checkbox_element.click()

    print("Clicking join button.")
    join_button_element = await page.wait_for_selector(
        'button[data-testid="button"][aria-label="Join"]'
    )
    await join_button_element.click()

    print("Opening chat panel.")
    try:
        chat_panel_element = await page.wait_for_selector(
            'button[data-testid="button"][aria-label^="Open chat panel"]',
            timeout=details.waiting_timeout
        )
    except TimeoutError:
        print("LMA Virtual Participant was not admitted into the meeting.")
        return
    else:
        await chat_panel_element.click()

    async def send_messages(messages):
        message_element = await page.wait_for_selector(
            'textarea[placeholder="Message all attendees"]'
        )
        for message in messages:
            await message_element.fill(message)
            await message_element.press('Enter')

    print("Sending introduction messages.")
    await send_messages(details.intro_messages)

    print("Opening attendees panel.")
    attendees_panel_element = await page.wait_for_selector(
        'button[data-testid="button"][aria-label^="Open attendees panel"]'
    )
    await attendees_panel_element.click()

    async def attendee_change(number: int):
        if number <= 1:
            print("LMA Virtual Participant got lonely and left.")
            details.start = False
            await page.goto("about:blank")

    await page.expose_function("attendeeChange", attendee_change)

    print("Listening for attendee changes.")
    await page.evaluate('''
        const targetNode = document.querySelector('button[data-testid="collapse-container"][aria-label^="Present"]')
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
        const targetNode = document.querySelector('.activeSpeakerCell ._3yg3rB2Xb_sfSzRXkm8QT-')
        const config = { characterData: true, subtree: true }

        const callback = (mutationList, observer) => {
            for (const mutation of mutationList) {
                const new_speaker = mutation.target.textContent
                if (new_speaker != "No one") speakerChange(new_speaker)
            }
        }

        const observer = new MutationObserver(callback)
        observer.observe(targetNode, config)

        const initial_speaker = targetNode.textContent
        if (initial_speaker != "No one") speakerChange(initial_speaker)
    ''')

    # start the transcription if details.start flag is true
    if details.start:
        print(details.start_messages[0])
        await send_messages(details.start_messages)
        asyncio.create_task(scribe.transcribe())

    async def message_change(sender, text, attachment_title, attachment_href):
        global prev_sender
        if not sender:
            sender = prev_sender
        prev_sender = sender
        if text == details.end_command:
            print("LMA Virtual Participant has been removed from the meeting.")
            await send_messages(details.exit_messages)
            details.start = False
            await page.goto("about:blank")
        elif details.start and text == details.pause_command:
            details.start = False
            print(details.pause_messages[0])
            await send_messages(details.pause_messages)
        elif not details.start and text == details.start_command:
            details.start = True
            print(details.start_messages[0])
            await send_messages(details.start_messages)
            asyncio.create_task(scribe.transcribe())
        elif details.start and not (sender == "Amazon Chime" or details.lma_identity in sender):
            timestamp = datetime.now().strftime('%H:%M')
            message = f"[{timestamp}] {sender}: "
            if attachment_title and attachment_href:
                details.attachments[attachment_title] = attachment_href
                if text:
                    message += f"{text} | {attachment_title}"
                else:
                    message += attachment_title
            else:
                message += text
            # print('New Message:', message)
            details.messages.append(message)

    await page.expose_function("messageChange", message_change)

    print("Listening for message changes.")
    await page.evaluate('''
        const targetNode = document.querySelector('._2B9DdDvc2PdUbvEGXfOU20')
        const config = { childList: true, subtree: true }

        const callback = (mutationList, observer) => {
            for (const mutation of mutationList) {
                const addedNode = mutation.addedNodes[0]
                if (addedNode) {
                    const sender = addedNode.querySelector('h3[data-testid="chat-bubble-sender-name"]')?.textContent
                    const text = addedNode.querySelector('.Linkify')?.textContent
                    const attachmentElement = addedNode.querySelector('.SLFfm3Dwo5MfFzks4uM11')
                    const attachmentTitle = attachmentElement?.title
                    const attachmentHref = attachmentElement?.href
                    messageChange(sender, text, attachmentTitle, attachmentHref)  
                }
            }
        }

        const observer = new MutationObserver(callback)
        observer.observe(targetNode, config)
    ''')

    print("Waiting for meeting end.")
    try:
        await page.wait_for_selector('button[id="endMeeting"]', state="detached", timeout=details.meeting_timeout)
        print("Meeting ended.")
    except TimeoutError:
        print("Meeting timed out.")
    finally:
        details.start = False
