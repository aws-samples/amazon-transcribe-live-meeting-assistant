import asyncio
import details
import scribe
from playwright.async_api import TimeoutError
from takescreenshots import take_and_upload_screenshot


async def wait_for_meeting_ui_ready(page, timeout=10000):
    """Wait for Zoom meeting UI to be ready by checking for multiple indicators"""
    try:
        # Wait for any combination of elements that indicate UI is ready
        await page.wait_for_selector(
            'button[aria-label="open the chat panel"], '
            '.footer-button__number-counter, '
            '[data-testid="chat-button"], '
            '.zm-video-container, '
            '.meeting-client-view, '
            '.participants-counter',
            timeout=timeout
        )
        return True
    except TimeoutError:
        print(f"UI readiness check timed out after {timeout}ms")
        return False


async def wait_for_element_with_fallbacks(page, selectors, timeout=5000):
    """Try multiple selectors with fallback options"""
    if isinstance(selectors, str):
        selectors = [selectors]
    
    for i, selector in enumerate(selectors):
        try:
            element = await page.wait_for_selector(selector, timeout=timeout if i == 0 else 2000)
            if element:
                return element
        except TimeoutError:
            if i < len(selectors) - 1:
                print(f"Selector '{selector}' not found, trying next...")
                continue
            else:
                print(f"All selectors failed: {selectors}")
                return None
    return None


async def meeting(page):

    print("Getting meeting link.")
    await page.goto(f"https://zoom.us/wc/{details.meeting_id}/join")

    print("Typing meeting password.")
    try:
        password_text_element = await page.wait_for_selector("#input-for-pwd")
    except TimeoutError:
        print("LMA Virtual Participant was unable to join the meeting.")
        return
    else:
        await password_text_element.type(details.meeting_password)

    print("Entering name.")
    try:
        name_text_element = await page.wait_for_selector("#input-for-name")
        await name_text_element.type(details.lma_identity)
        await name_text_element.press("Enter")
    except TimeoutError:
        print("LMA Virtual Participant was not admitted into the meeting.")
        return
    # Wait for meeting interface to load by checking for key UI elements
    print("Waiting for meeting interface to load...")
    try:
        # Wait for any of these key elements that indicate the meeting UI is ready
        await page.wait_for_selector(
            'button[aria-label="open the chat panel"], '
            '.footer-button__number-counter, '
            '[data-testid="chat-button"], '
            '.zm-video-container, '
            '.meeting-client-view',
            timeout=15000
        )
        print("Meeting interface loaded successfully")
    except TimeoutError:
        print("Meeting interface took longer than expected to load, continuing...")

    # First, check for and dismiss any modal overlays that might be blocking interactions
    try:
        # Look for microphone/camera permission modal and try to enable microphone for audio reception
        try:
            # Try different button combinations based on what's available
            mic_enabled = False

            # First try "Use microphone and camera"
            try:
                use_mic_cam_btn = await page.wait_for_selector('text="Use microphone and camera"', timeout=2000)
                if use_mic_cam_btn:
                    await use_mic_cam_btn.click()
                    mic_enabled = True
            except:
                pass

            # If that didn't work, try just "Use microphone"
            if not mic_enabled:
                try:
                    use_mic_btn = await page.wait_for_selector('text="Use microphone"', timeout=2000)
                    if use_mic_btn:
                        await use_mic_btn.click()
                        mic_enabled = True
                except:
                    pass
            
            # Wait for permission processing by checking if modal is gone or chat button appears
            try:
                await page.wait_for_selector(
                    'button[aria-label="open the chat panel"], .footer-button__number-counter',
                    timeout=3000
                )
            except TimeoutError:
                await page.wait_for_timeout(1000)  # Short fallback

            # If microphone couldn't be enabled, fall back to continue without
            if not mic_enabled:
                try:
                    continue_without_btn = await page.wait_for_selector('text="Continue without microphone and camera"', timeout=2000)
                    if continue_without_btn:
                        await continue_without_btn.click()

                        # Wait for modal to disappear and main UI to appear
                        await wait_for_meeting_ui_ready(page)
                        
                        # Check if there's another modal and click again if needed
                        try:
                            continue_without_btn2 = await page.wait_for_selector('text="Continue without microphone and camera"', timeout=1000)
                            if continue_without_btn2:
                                await continue_without_btn2.click()
                                await wait_for_meeting_ui_ready(page)
                        except:
                            pass
                except:
                    pass

        except Exception as e:
            print(f"Error handling microphone/camera modals: {e}")

        # Check for other general modals
        modal_selectors = [
            '.ReactModal__Overlay',
            '.zm-modal',
            '.modal-overlay',
            '[role="dialog"]'
        ]

        for modal_selector in modal_selectors:
            try:
                modal = await page.wait_for_selector(modal_selector, timeout=2000)
                if modal:
                    # Try to find and click close button
                    close_selectors = [
                        'button[aria-label="Close"]',
                        'button[aria-label="close"]',
                        '.close-button',
                        '.modal-close',
                        'button:has-text("Close")',
                        'button:has-text("OK")',
                        'button:has-text("Got it")',
                        'text="Continue without microphone and camera"'
                    ]

                    modal_closed = False
                    for close_selector in close_selectors:
                        try:
                            close_btn = await page.wait_for_selector(close_selector, timeout=1000)
                            if close_btn:
                                await close_btn.click()
                                modal_closed = True
                                break
                        except:
                            continue

                    if not modal_closed:
                        # Try pressing Escape key
                        await page.keyboard.press('Escape')
                    break
            except:
                continue
    except Exception as e:
        print(f"Error handling modals: {e}")

    print("Opening chat panel.")
    # Try multiple selectors for chat button (Zoom UI changes frequently)
    chat_selectors = [
        'button[aria-label="open the chat panel"]',
        'button[aria-label="Open chat panel"]',
        '[data-testid="chat-button"]',
        'button[title*="Chat"]',
        'button:has-text("Chat")',
        '.chat-button'
    ]
    
    chat_button_element = await wait_for_element_with_fallbacks(page, chat_selectors, 10000)
    if not chat_button_element:
        print("Could not find chat button - continuing without chat")
        return
    
    try:
        await chat_button_element.hover()
        await chat_button_element.click()
        print("Chat panel opened successfully")
    except Exception as e:
        print(f"Error clicking chat button: {e} - continuing without chat")
        return

    async def send_messages(messages):
        # Try multiple selectors for message input
        message_selectors = [
            'div[aria-placeholder="Type message here ..."]',
            'div[aria-placeholder="Type message here..."]', 
            'textarea[placeholder="Type message here ..."]',
            '[data-testid="chat-input"]',
            '.chat-input',
            'input[placeholder*="message"]'
        ]
        
        message_element = await wait_for_element_with_fallbacks(page, message_selectors, 10000)
        if not message_element:
            print("Could not find message input element")
            return
            
        try:
            for message in messages:
                await message_element.fill(message)
                await message_element.press("Enter")
                await page.wait_for_timeout(500)  # Brief pause between messages
        except Exception as e:
            print(f"Error sending message: {e}")

    print("Sending introduction messages.")
    await send_messages(details.intro_messages)

    async def attendee_change(number: int):
        if number <= 1:
            print("LMA Virtual Participant got lonely and left.")
            details.start = False
            await page.goto("about:blank")

    await page.expose_function("attendeeChange", attendee_change)

    print("Listening for attendee changes.")
    await page.evaluate(
        """
        const targetNode = document.querySelector('.footer-button__number-counter')
        const config = { characterData: true, subtree: true }

        const callback = (mutationList, observer) => {
            attendeeChange(parseInt(mutationList[mutationList.length - 1].target.textContent))
        }

        const observer = new MutationObserver(callback)
        observer.observe(targetNode, config)
    """
    )

    await page.expose_function("speakerChange", scribe.speaker_change)

    print("Listening for speaker changes.")
    await page.evaluate(
        """
        // NOTE: This is not yet correct.  Works in both Personal and Enterprise, but doesn't respond to speaker changes
        const targetNode = document.querySelector(
            '.speaker-active-container__video-frame > .video-avatar__avatar > .video-avatar__avatar-footer'
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
    """
    )

    # start the transcription if details.start flag is true
    if details.start:
        print(details.start_messages[0])
        await send_messages(details.start_messages)
        asyncio.create_task(scribe.transcribe())

    async def message_change(message):
        print("New Message:", message)
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
    await page.evaluate(
        """
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
    """
    )

    print("Waiting for meeting end.")
    try:
        done, pending = await asyncio.wait(
            fs=[
                asyncio.create_task(
                    page.wait_for_selector(
                        'button[aria-label="Leave"],[aria-label="End"]',
                        state="detached",
                        timeout=0,
                    )
                ),
                asyncio.create_task(
                    page.wait_for_selector(
                        # 'div[class="zm-modal zm-modal-legacy"]', timeout=0
                        # Note - recent UI changes may result in clicking the "End Meeting for All" as opposed to "Leave Meeting"
                        "button.leave-meeting-options__btn",
                        timeout=0,
                    )
                ),
                asyncio.create_task(
                    page.wait_for_selector(
                        'text="This meeting has been ended by host"',
                        timeout=0,
                    )
                ),
                asyncio.create_task(
                    page.wait_for_url("about:blank", timeout=0)
                ),
            ],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=details.meeting_timeout,
        )
        [task.cancel() for task in pending]
        print("Meeting ended.")
    except:
        print("Meeting timed out.")
    finally:
        details.start = False
