import { Page, Frame } from 'puppeteer';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';
import { createStatusManager } from "./status-manager.js";

export default class Webex {
    private readonly iframe = '#unified-webclient-iframe';
    private async sendMessages(
        frame: Frame,
        messages: string[]
    ): Promise<void> {
        // Try normal Webex chat input (Quill editor)
        let messageElement = await frame.$('.ql-editor[contenteditable="true"]');
        
        // If not found, try enterprise Webex chat input (textarea)
        if (!messageElement) {
            console.log('Standard chat input not found, trying enterprise selector...');
            messageElement = await frame.$('textarea[placeholder="Type your message here"]');
        }
        
        if (!messageElement) {
            throw new Error('Chat input field not found with any selector');
        }
        
        for (const message of messages) {
            await messageElement.type(message);
            await messageElement.press('Enter');
            await new Promise(resolve => setTimeout(resolve, 500)); // Small delay between messages
        }
        console.log('Sent messages:', messages);
    }

    public async initialize(page: Page): Promise<void> {
        console.log('Getting Webex meeting link.');
        await page.goto('https://signin.webex.com/join');
        console.log('Entering meeting ID.');
        const meetingTextElement = await page.waitForSelector('#join-meeting-form');
        await meetingTextElement?.type(details.invite.meetingId);
        await meetingTextElement?.press('Enter');
        
        // Wait for page to stabilize and load after entering meeting ID
        await new Promise(resolve => setTimeout(resolve, 5000));
        
        console.log(`Current URL after entering meeting ID: ${page.url()}`);
        
        // Try to click "Join from this browser" button if it appears
        // Sometimes Webex skips this step and goes directly to the meeting join page
        console.log('Checking for "Join from this browser" button...');
        try {
            const joinFromBrowserButton = await page.waitForSelector('#broadcom-center-right', { timeout: 5000 });
            if (joinFromBrowserButton) {
                console.log('Found "Join from this browser" button, clicking it.');
                await joinFromBrowserButton.click();
                // Wait for the page to load after clicking
                await new Promise(resolve => setTimeout(resolve, 5000));
                console.log(`URL after clicking "Join from browser": ${page.url()}`);
            }
        } catch (error) {
            console.log('"Join from this browser" button not found - Webex may have auto-detected browser mode. Continuing...');
        }
        
        // Handle enterprise Webex guest form (name/email) if present
        // The form is inside iframe[name="thinIframe"]
        let usedEnterpriseFlow = false;
        console.log('Checking for enterprise Webex guest form in thinIframe...');
        try {
            const thinIframeElement = await page.waitForSelector('iframe[name="thinIframe"]', { timeout: 10000 });
            if (thinIframeElement) {
                console.log('Found thinIframe, checking for guest form...');
                const thinIframe = await thinIframeElement.contentFrame();
                
                if (thinIframe) {
                    // Wait a moment for iframe content to load
                    await new Promise(resolve => setTimeout(resolve, 2000));
                    
                    // Check for guest form (name/email)
                    const nameInput = await thinIframe.$('input[aria-labelledby="nameLabel"]');
                    console.log(`Guest form name input found: ${nameInput !== null}`);
                    
                    if (nameInput) {
                        usedEnterpriseFlow = true;
                        console.log('Enterprise Webex guest form detected, auto-filling name and email...');
                        const emailInput = await thinIframe.$('input[aria-labelledby="emailLabel"]');
                        
                        await nameInput.type(details.scribeIdentity);
                        
                        // Create a valid email from lmaUser
                        let userEmail: string;
                        if (details.lmaUser.includes('@')) {
                            // Already has @, use as-is
                            userEmail = details.lmaUser;
                        } else {
                            // Sanitize username: keep only alphanumeric, dots, hyphens, underscores
                            const sanitizedUser = details.lmaUser.replace(/[^a-zA-Z0-9._-]/g, '-');
                            userEmail = `${sanitizedUser}@example.com`;
                        }
                        
                        await emailInput?.type(userEmail);
                        console.log(`Filled name: "${details.scribeIdentity}", email: "${userEmail}"`);
                        
                        // Wait for form validation to complete
                        await new Promise(resolve => setTimeout(resolve, 2000));
                        
                        console.log('Clicking Next button...');
                        const nextButton = await thinIframe.$('#guest_next-btn');
                        await nextButton?.click();
                        
                        // Wait for password page to load
                        await new Promise(resolve => setTimeout(resolve, 5000));
                        
                        // Handle meeting password if present
                        console.log('Checking for meeting password field...');
                        const passwordInput = await thinIframe.$('input[type="password"]');
                        if (passwordInput && details.invite.meetingPassword) {
                            console.log('Password field detected, auto-filling meeting password...');
                            await passwordInput.type(details.invite.meetingPassword);
                            
                            console.log('Clicking Next button after password...');
                            const passwordNextButton = await thinIframe.$('#password_validate_btn');
                            await passwordNextButton?.click();
                            
                            // Wait for video settings page to load
                            console.log('Waiting for video settings page...');
                            await new Promise(resolve => setTimeout(resolve, 5000));
                            
                            // Handle video settings page (mute audio, stop video, join meeting)
                            console.log('Looking for audio control button...');
                            const audioButton = await thinIframe.$('#audioControlButton');
                            if (audioButton) {
                                const audioButtonText = await audioButton.evaluate((el: Element) => el.textContent || '');
                                console.log(`Audio button text: "${audioButtonText}"`);
                                if (audioButtonText.includes('Mute')) {
                                    console.log('Clicking Mute button...');
                                    await audioButton.click();
                                    await new Promise(resolve => setTimeout(resolve, 1000));
                                } else {
                                    console.log('Audio already muted');
                                }
                            } else {
                                console.log('Audio control button not found');
                            }
                            
                            console.log('Looking for video control button...');
                            const videoButton = await thinIframe.$('button[data-doi="VIDEO:STOP_VIDEO:MEETSIMPLE_INTERSTITIAL"]');
                            if (videoButton) {
                                const videoButtonText = await videoButton.evaluate((el: Element) => el.textContent || '');
                                console.log(`Video button text: "${videoButtonText}"`);
                                if (videoButtonText.includes('Stop video')) {
                                    console.log('Clicking Stop video button...');
                                    await videoButton.click();
                                    await new Promise(resolve => setTimeout(resolve, 1000));
                                } else {
                                    console.log('Video already stopped');
                                }
                            } else {
                                console.log('Video control button not found');
                            }
                            
                            console.log('Clicking Join meeting button...');
                            const joinButton = await thinIframe.$('#interstitial_join_btn');
                            await joinButton?.click();
                            
                            // Wait for meeting to load
                            console.log('Waiting for meeting to load after enterprise flow...');
                            await new Promise(resolve => setTimeout(resolve, 10000));
                        }
                    }
                }
            }
        } catch (error) {
            console.log(`No enterprise guest form detected in thinIframe, continuing with normal flow...`);
        }
        
        // If we used enterprise flow, skip the normal unified-webclient-iframe flow
        // and go directly to finding the meeting frame
        let frame: Frame | null = null;
        
        if (!usedEnterpriseFlow) {
            // Normal flow: wait for unified-webclient-iframe and enter name/mute/etc
            console.log('Using normal Webex flow...');
            await new Promise(resolve => setTimeout(resolve, 2000));
            console.log('Launching app.');
            const frameElement = await page.waitForSelector(this.iframe, { timeout: 15000 });
            frame = (await frameElement?.contentFrame()) || null;
            if (!frame) {
                throw new Error('Failed to access Webex meeting frame');
            }
            await page.evaluate(() => {
                const checkAndClosePopup = () => {
                    const dialog = document.querySelector('.el-dialog__wrapper');
                    if (dialog && dialog.textContent?.includes('Problem joining from browser?')) {
                        const closeButton = dialog.querySelector('.el-dialog__close');
                        if (closeButton) {
                            console.log('Auto-closing "Problem joining from browser?" popup');
                            (closeButton as HTMLElement).click();
                            return true;
                        }
                    }
                    return false;
                };

                // Check immediately
                if (!checkAndClosePopup()) {
                    const observer = new MutationObserver(() => {
                        if (checkAndClosePopup()) {
                            observer.disconnect();
                        }
                    });
                    observer.observe(document.body, { childList: true, subtree: true });
                }
            });
            console.log('Entering name in the new interface.');
            console.log(frame.evaluate(()=> document.querySelector('input[data-test="Name (required)"]')));
            const nameInputElement = await frame.waitForSelector('input[data-test="Name (required)"]',{ timeout: 10000 });
            await nameInputElement?.type(details.scribeIdentity);

            // Wait for the meeting interface to load
            await new Promise(resolve => setTimeout(resolve, 1000));
            // need to change frame for this.
            console.log('Handling cookie banner.');
            try {
                const rejectButton = await page.waitForSelector(
                    '.cookie-banner-body .a32ueaoVYHwRrsRMl0ci mdc-button:first-child',
                    { timeout: 3000 }
                );
                await rejectButton?.click();
                console.log('Successfully clicked Reject cookie button');
            } catch (error) {
                console.log('Cookie banner not found:', error);
            }
            console.log('Clicking mute button.');
            const muteButtonElement = await frame.waitForSelector('mdc-button[data-test="microphone-button"]');
            await muteButtonElement?.click();

            console.log('Clicking video button.');
            const videoButtonElement = await frame.waitForSelector(
                'mdc-button[data-test="camera-button"]'
            );
            await videoButtonElement?.click();

            console.log('Clicking join button.');
            const joinButtonElement = await frame.waitForSelector('mdc-button[data-test="join-button"]');
            await joinButtonElement?.click();
        } else {
            // Enterprise flow: meeting should already be loading, find the meeting frame
            console.log('Enterprise flow completed, looking for meeting frame...');
            // The meeting frame might still be in thinIframe or might be unified-webclient-iframe
            const thinIframeElement = await page.$('iframe[name="thinIframe"]');
            if (thinIframeElement) {
                frame = (await thinIframeElement.contentFrame()) || null;
            }
            if (!frame) {
                // Try unified-webclient-iframe as fallback
                const frameElement = await page.waitForSelector(this.iframe, { timeout: 15000 }).catch(() => null);
                if (frameElement) {
                    frame = (await frameElement.contentFrame()) || null;
                }
            }
            if (!frame) {
                throw new Error('Failed to access Webex meeting frame after enterprise flow');
            }
            
            // For enterprise Webex, mute/stop video again in the actual meeting
            // (the pre-join settings don't always persist)
            console.log('Ensuring audio is muted in meeting...');
            try {
                const muteButton = await frame.waitForSelector('button[aria-label="Mute"]', { timeout: 5000 });
                if (muteButton) {
                    const buttonText = await muteButton.evaluate((el: Element) => el.textContent || '');
                    if (buttonText.includes('Mute')) {
                        console.log('Clicking Mute button in meeting...');
                        await muteButton.click();
                        await new Promise(resolve => setTimeout(resolve, 1000));
                    }
                }
            } catch (error) {
                console.log('Could not mute audio in meeting:', error);
            }
            
            console.log('Ensuring video is stopped in meeting...');
            try {
                const stopVideoButton = await frame.waitForSelector('button[aria-label="Stop video"]', { timeout: 5000 });
                if (stopVideoButton) {
                    console.log('Clicking Stop video button in meeting...');
                    await stopVideoButton.click();
                    await new Promise(resolve => setTimeout(resolve, 1000));
                }
            } catch (error) {
                console.log('Could not stop video in meeting:', error);
            }
        }

        console.log("Opening chat panel.");
        let chatPanelOpened = false;
        try {
            // Try standard selector first
            let chatButton = await frame.waitForSelector('mdc-button[data-test="in-meeting-chat-toggle-button"]', {
                timeout: 5000,
            }).catch(() => null);
            
            // If not found, try enterprise Webex selector
            if (!chatButton) {
                console.log('Standard chat button not found, trying enterprise selector...');
                chatButton = await frame.waitForSelector('button[aria-label="Chat panel"]', {
                    timeout: 5000,
                }).catch(() => null);
            }
            
            if (chatButton) {
                await chatButton.click();
                console.log("Chat panel button clicked successfully");
                chatPanelOpened = true;
                
                // Wait for chat panel to fully open and input field to be ready
                await new Promise(resolve => setTimeout(resolve, 3000));
            } else {
                console.log("Chat panel button not found with any selector");
            }
        } catch(error: any) {
            console.log(`Chat panel error: ${error.message}`);
        }

        if (details.invite.virtualParticipantId) {
            const statusManager = createStatusManager(details.invite.virtualParticipantId);
            await statusManager.setJoined();
        }
        console.log('Successfully joined Webex meeting');

        // Send introduction messages only if chat panel is available
        if (chatPanelOpened) {
            console.log('Sending introduction messages.');
            try {
                await this.sendMessages(frame, details.introMessages);
            } catch (error) {
                console.log('Failed to send introduction messages:', error);
            }
        } else {
            console.log('Skipping introduction messages (chat panel not available)');
        }

        // Set up speaker change monitoring
        await page.exposeFunction('speakerChange', async (speaker: string) => {
            await transcriptionService.speakerChange(speaker);
        });
        console.log("Listening for speaker changes.");
        await frame.evaluate(() => {
              const doc = document;

              // --- Helpers ------------------------------------------------------------
              // Accept both Document and Element for flexibility
              const NAME_SELECTORS = [
                '[data-test="participant-name"]',
                '[class*="full-name"]',
                'mdc-text[type="body-large-regular"]',
                'mdc-text',
                '[class*="name"]',
                '[data-test*="name"]',
              ];

              function getActiveSpeakerElement(root: Document | Element): Element | null {
                // querySelector exists on both Document and Element
                return root.querySelector?.('.active-speaker-halo') ?? null;
              }

              function getParticipantItem(node: Element | null): Element | null {
                if (!node) return null;
                return (
                  node.closest?.(
                    'li,[role="listitem"],.participants-list-item,.participants-video-tile,.participants-video-panel-wrapper'
                  ) ?? null
                );
              }

              function extractNameFromItem(item: Element | null): string | null {
                  if (!item) return null;

                  for (const sel of NAME_SELECTORS) {
                      const el = item.querySelector?.(sel) as Element | null;
                      const text = el?.textContent?.trim();
                      if (text) return text;
                  }
                  const aria = item.getAttribute?.('aria-label')?.trim();
                  if (aria) return aria;
                  const text = item.textContent?.trim();
                  return text || null;
              }
              function getActiveSpeakerName(): string | null {
                const halo = getActiveSpeakerElement(doc);
                if (!halo) return null;
                const item = getParticipantItem(halo);
                return extractNameFromItem(item);
              }
              let lastAnnounced = "";
              function announceIfChanged() {
                const name = getActiveSpeakerName();
                if (name && name !== lastAnnounced) {
                  lastAnnounced = name;
                  console.log(`Speaker changed to: ${name}`);
                  (window as any).speakerChange?.(name);
                }
              }
              // Initial scan
              announceIfChanged();
              const observer = new MutationObserver((mutations) => {
                for (const m of mutations) {
                  if (m.type === 'childList') {
                    // Added/removed/moved halo?
                    for (const n of [...m.addedNodes, ...m.removedNodes]) {
                      if (
                        n instanceof Element &&
                        (n.matches?.('.active-speaker-halo') || n.querySelector?.('.active-speaker-halo'))
                      ) {
                        announceIfChanged();
                        return;
                      }
                    }
                  } else if (m.type === 'attributes') {
                    const t = m.target as Element;
                    if (
                      t.matches?.('.active-speaker-halo') ||
                      t.classList?.contains?.('active-speaker-halo') ||
                      t.querySelector?.('.active-speaker-halo')
                    ) {
                      announceIfChanged();
                      return;
                    }
                  }
                }
              });

              observer.observe(doc.body, {
                subtree: true,
                childList: true,
                attributes: true,
                attributeFilter: ['class', 'style', 'hidden', 'aria-hidden'],
              });

              const interval = setInterval(announceIfChanged, 500);

              (window as any).__webexActiveSpeakerStop = () => {
                observer.disconnect();
                clearInterval(interval);
              };
            });
        // Set up message monitoring with LMA features
        await page.exposeFunction('messageChange', async (message: string) => {
            if (message.includes(details.endCommand)) {
                console.log('LMA Virtual Participant has been removed from the meeting.');
                await this.sendMessages(frame, details.exitMessages);
                details.start = false;
                await page.goto('about:blank');
            } else if (
                details.start &&
                message.includes(details.pauseCommand)
            ) {
                details.start = false;
                console.log(details.pauseMessages[0]);
                await this.sendMessages(frame, details.pauseMessages);
            } else if (
                !details.start &&
                message.includes(details.startCommand)
            ) {
                details.start = true;
                console.log(details.startMessages[0]);
                await this.sendMessages(frame, details.startMessages);
                // Restart transcription if needed
                transcriptionService.startTranscription();
            } else if (details.start) {
                // Process meeting messages (LMA feature)
                const timestamp = new Date().toLocaleTimeString('en-US', {
                    hour12: false,
                    hour: '2-digit',
                    minute: '2-digit',
                });
                const formattedMessage = `[${timestamp}] ${message}`;
                details.messages.push(formattedMessage);
                console.log('New message:', formattedMessage);
            }
        });

        console.log('Listening for message changes.');
        await frame.evaluate(() => {
                const targetNode = document.querySelector(
                    'div[class^="style-chat-box"]'
                );

                const config = { childList: true, subtree: true };

                const callback = (mutationList: MutationRecord[]) => {
                    const lastMutation = mutationList[mutationList.length - 1];
                    const addedNode = lastMutation.addedNodes[0] as Element;
                    if (addedNode) {
                        const sender = addedNode.querySelector(
                            'h3[class^="style-chat-label"]'
                        )?.textContent;
                        const message = addedNode.querySelector(
                            'span[class^="style-chat-msg"]'
                        )?.textContent;
                        if (!sender!.startsWith('from LMA')) {
                            (window as any).messageChange(message);
                        }
                    }
                };

                const observer = new MutationObserver(callback);
                if (targetNode) observer.observe(targetNode, config);
            });

        // Start transcription if enabled
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(frame, details.startMessages);
            transcriptionService.startTranscription();
        }

        console.log('Waiting for meeting end.');
        try {
            // Set up meeting end detection by monitoring iframe text content
            let meetingEnded = false;
            await page.exposeFunction('meetingEndDetected', async (reason: string) => {
                if (!meetingEnded) {
                    meetingEnded = true;
                    console.log(`Meeting ended detected: ${reason}`);
                    details.start = false;
                }
            });

            // Monitor the iframe for meeting end text
            await frame.evaluate(() => {
                    const observer = new MutationObserver(() => {
                        const bodyText = document.body?.textContent?.toLowerCase() || '';

                        if (bodyText.includes('meeting has ended') ||
                            bodyText.includes('this meeting has ended') ||
                            bodyText.includes('meeting ended') ||
                            bodyText.includes('you have left the meeting') ||
                            bodyText.includes('meeting disconnected')) {
                            (window as any).meetingEndDetected('Meeting end text detected');
                            observer.disconnect();
                        }
                    });

                    observer.observe(document.body, {
                        childList: true,
                        subtree: true,
                        characterData: true
                    });

                    // Check immediately in case meeting already ended
                    const bodyText = document.body?.textContent?.toLowerCase() || '';
                    if (bodyText.includes('meeting has ended') ||
                        bodyText.includes('this meeting has ended') ||
                        bodyText.includes('meeting ended')) {
                        (window as any).meetingEndDetected('Meeting end text detected immediately');
                        observer.disconnect();
                    }
                });

            // Wait for meeting end or timeout
            const startTime = Date.now();
            while (!meetingEnded && (Date.now() - startTime) < details.meetingTimeout) {
                await new Promise(resolve => setTimeout(resolve, 1000)); // Check every second
            }

            if (!meetingEnded) {
                console.log('Meeting timed out.');
            }
            console.log('Meeting ended.');
        } catch (error) {
            console.log('Meeting ended with error:', error);
        } finally {
            details.start = false;
        }
    }
}
