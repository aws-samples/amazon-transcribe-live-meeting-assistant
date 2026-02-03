import { Page, Frame } from 'puppeteer';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';
import { createStatusManager } from "./status-manager.js";

export default class Webex {
    private readonly iframe = '#unified-webclient-iframe';
    private async sendMessages(
        frame: Frame,
        messages: string[],
        isEnterprise: boolean | null = false
    ): Promise<void> {
        const messageElement = await frame.waitForSelector(isEnterprise ? '#chat-panel > div > textarea' : '.ql-editor[contenteditable="true"]');
        for (const message of messages) {
            await messageElement?.type(message);
            await messageElement?.press('Enter');
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
        
        // Wait a moment for the page to stabilize after entering meeting ID
        await new Promise(resolve => setTimeout(resolve, 2000));
        
        // Try to click "Join from this browser" button if it appears
        // Sometimes Webex skips this step and goes directly to the meeting join page
        console.log('Checking for "Join from this browser" button...');
        try {
            const joinFromBrowserButton = await page.waitForSelector('#broadcom-center-right', { timeout: 5000 });
            if (joinFromBrowserButton) {
                console.log('Found "Join from this browser" button, clicking it.');
                await joinFromBrowserButton.click();
                // Wait for the page to load after clicking
                await new Promise(resolve => setTimeout(resolve, 3000));
            }
        } catch (error) {
            console.log('"Join from this browser" button not found - Webex may have auto-detected browser mode. Continuing...');
        }
        
        // Wait for the page to stabilize
        await new Promise(resolve => setTimeout(resolve, 2000));
        console.log('Launching app.');
        const frameElement = await Promise.any([
            page.waitForSelector(this.iframe, { timeout: 15000 }).then((el: any) => ({ source: 'default', el })).catch(() => null),
            page.waitForSelector('iframe[name="thinIframe"]', { timeout: 15000 }).then((el: any) => ({ source: 'enterprise', el })).catch(() => null)
        ]).catch(() => null);

        const frame = await frameElement?.el?.contentFrame();
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

        const passwordCheckEl = await Promise.any([
            frame.waitForSelector('input[aria-label="Meeting password"]', { timeout: 30000 }).then((el: any) => ({ source: 'password', el })).catch(() => null),
            frame.waitForSelector('input[data-test="Name (required)"]', { timeout: 30000 }).then((el: any) => ({ source: 'name', el })).catch(() => null),
            frame.waitForSelector('input[aria-labelledby="nameLabel"]', { timeout: 30000 }).then((el: any) => ({ source: 'enterprise-name', el })).catch(() => null)
        ]).catch(() => null);
    
        // Handle password page if detected
        if (passwordCheckEl && passwordCheckEl.source === 'password') {
            const passwordInput = passwordCheckEl.el;
            
            // Check if password is required and available
            if (details.invite.meetingPassword) {
                console.log('Auto-filling meeting password...');
                if (passwordInput) {
                    await passwordInput.type(details.invite.meetingPassword);
                }
            } else {
                console.log('ERROR: Meeting requires password but none was provided.');
                throw new Error('Meeting requires password but none was provided in invite details');
            }
            
            // Check for CAPTCHA
            const captchaImage = await frame.$('img[alt="Captcha image"]');
            if (captchaImage) {
                console.log('CAPTCHA detected! Triggering human-in-the-loop...');
                
                // Notify frontend that manual action is required
                if (details.invite.virtualParticipantId) {
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.setManualActionRequired(
                        'CAPTCHA',
                        'CAPTCHA detected on Webex password page. Please solve the CAPTCHA in the VNC viewer and click Next.',
                        120
                    );
                }
                
                // Wait for CAPTCHA to be solved (Next button to be enabled and clicked, or name input to appear)
                console.log('Waiting for CAPTCHA to be solved (up to 2 minutes)...');
                await Promise.race([
                    // Wait for name input to appear (successful CAPTCHA solve + Next click)
                    frame.waitForSelector('input[data-test="Name (required)"]', {
                        timeout: 120000,
                        visible: true
                    }),
                    // Or wait for the Next button to be clicked (we'll detect by it disappearing)
                    frame.waitForFunction(
                        () => {
                            const nextBtn = document.querySelector('mdc-button[type="submit"]');
                            return !nextBtn || nextBtn.getAttribute('disabled') === null;
                        },
                        { timeout: 120000 }
                    )
                ]);
                
                console.log('CAPTCHA appears to be resolved, continuing...');
                await new Promise((resolve) => setTimeout(resolve, 2000));
                
                // Clear manual action notification after CAPTCHA is resolved
                if (details.invite.virtualParticipantId) {
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.clearManualAction();
                }
            } else {
                // No CAPTCHA, just password - click Next button
                console.log('No CAPTCHA detected, clicking Next button...');
                const nextButton = await frame.waitForSelector('mdc-button[type="submit"]', { timeout: 5000 });
                await nextButton?.click();
                await new Promise((resolve) => setTimeout(resolve, 2000));
            }
        }

        console.log('Entering name (and email on enterprise)');
        // console.log(await frame.evaluate(()=> document.querySelector('input[data-test="Name (required)"]')));
        if (frameElement && frameElement.source === 'enterprise' && passwordCheckEl.source === 'enterprise-name') {
            // Check for guest form (name/email)
            const nameInput = await frame.$('input[aria-labelledby="nameLabel"]');
            console.log(`Guest form name input found: ${nameInput !== null}`);
            
            if (nameInput) {
                console.log('Enterprise Webex guest form detected, auto-filling name and email...');
                const emailInput = await frame.$('input[aria-labelledby="emailLabel"]');
                
                await nameInput.type(details.scribeIdentity);
                
                // Create a valid email from lmaUser
                let userEmail: string;
                if (details.lmaUser.includes('@')) {
                    // Already has @, use as-is
                    // userEmail = details.lmaUser; // enterprise emails redirect to SSO requiring login
                    const sanitizedUser = details.lmaUser.split('@')[0].replace(/[^a-zA-Z0-9._-]/g, '-');
                    userEmail = `${sanitizedUser}@example.com`;
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
                const nextButton = await frame.$('#guest_next-btn');
                await nextButton?.click();
                
                // Wait for password page to load
                await new Promise(resolve => setTimeout(resolve, 5000));
                
                // Handle meeting password if present
                console.log('Checking for meeting password field...');
                const passwordInput = await frame.$('input[type="password"]');
                if (passwordInput && details.invite.meetingPassword) {
                    console.log('Password field detected, auto-filling meeting password...');
                    await passwordInput.type(details.invite.meetingPassword);
                    
                    console.log('Clicking Next button after password...');
                    const passwordNextButton = await frame.$('#password_validate_btn');
                    await passwordNextButton?.click();
                }
            }
        } else {
            const nameInputElement = (passwordCheckEl && passwordCheckEl.source === 'name') ? passwordCheckEl.el : await frame.waitForSelector('input[data-test="Name (required)"]',{ timeout: 30000 });
            await nameInputElement?.type(details.scribeIdentity);
        }

        // Wait for the meeting interface to load
        await new Promise(resolve => setTimeout(resolve, 1000));
        // need to change frame for this.
        console.log('Handling cookie banner.');
        try {
            const rejectButton = await page.waitForSelector(
                (frameElement && frameElement.source === 'enterprise') ? '#cookie-banner-text > div.cookie-manage-option > div.cookie-banner-btnContainer > button:nth-child(1)' : '.cookie-banner-body .a32ueaoVYHwRrsRMl0ci mdc-button:first-child',
                { timeout: 3000 }
            );
            await rejectButton?.click();
            console.log('Successfully clicked Reject cookie button');
        } catch (error) {
            console.log('Cookie banner not found:', error);
        }

        console.log('Clicking mute button.');
        const muteButtonElement = await frame.waitForSelector((frameElement && frameElement.source === 'enterprise') ? '#audioControlButton' : 'mdc-button[data-test="microphone-button"]');
        await (frameElement && frameElement.source === 'enterprise') ? frame.evaluate((el: any) => el.click(), muteButtonElement) : muteButtonElement?.click();

        console.log('Clicking video button.');
        const videoButtonElement = await frame.waitForSelector(
            (frameElement && frameElement.source === 'enterprise') ? 'button[data-doi="VIDEO:STOP_VIDEO:MEETSIMPLE_INTERSTITIAL"]' : 'mdc-button[data-test="camera-button"]'
        );
        await (frameElement && frameElement.source === 'enterprise') ? frame.evaluate((el: any) => el.click(), videoButtonElement) : videoButtonElement?.click();

        console.log('Clicking join button.');
        const joinButtonElement = await frame.waitForSelector((frameElement && frameElement.source === 'enterprise') ? '#interstitial_join_btn' : 'mdc-button[data-test="join-button"]');
        await joinButtonElement?.click();

        console.log("Opening chat panel.");
        try {
            const chatToggleButton = (frameElement && frameElement.source === 'enterprise') ? 'button[data-doi="CHAT:OPEN_CHAT_PANEL:MENU_CONTROL_BAR"]' : 'mdc-button[data-test="in-meeting-chat-toggle-button"]';
            await frame.waitForSelector(chatToggleButton, {
                timeout: details.waitingTimeout,
            });
            await frame.click(chatToggleButton);
            console.log("Chat panel button clicked successfully");
        } catch(error: any) {
            console.log("Chat panel button error:", error.message);
            console.log("Your scribe was not admitted into the meeting.");
            return;
        }

        await new Promise(resolve => setTimeout(resolve, 1000));

        if (details.invite.virtualParticipantId) {
            const statusManager = createStatusManager(details.invite.virtualParticipantId);
            await statusManager.setJoined();
        }
        console.log('Successfully joined Webex meeting');

        console.log('Sending introduction messages.');
        await this.sendMessages(frame, details.introMessages, frameElement && frameElement.source === 'enterprise');

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
                '.videoitem-full-name-content-TzQC4', // Enterprise Webex
                '[class*="full-name"]',
                'mdc-text[type="body-large-regular"]',
                'mdc-text',
                '[class*="name"]',
                '[data-test*="name"]',
              ];

              function getActiveSpeakerElement(root: Document | Element): Element | null {
                // Try enterprise Webex first (speaking indicator class)
                const enterpriseSpeaker = root.querySelector?.('.videoitem-in-speaking-3a-w-');
                if (enterpriseSpeaker) return enterpriseSpeaker;
                
                // Fall back to normal Webex (active speaker halo)
                return root.querySelector?.('.active-speaker-halo') ?? null;
              }

              function getParticipantItem(node: Element | null): Element | null {
                if (!node) return null;
                
                // For enterprise Webex, the node IS the video item container
                if (node.classList?.contains?.('videoitem-in-speaking-3a-w-')) {
                  return node;
                }
                
                // For normal Webex, find the closest participant container
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
                    // Added/removed/moved halo or enterprise speaking indicator?
                    for (const n of [...m.addedNodes, ...m.removedNodes]) {
                      if (
                        n instanceof Element &&
                        (n.matches?.('.active-speaker-halo') ||
                         n.querySelector?.('.active-speaker-halo') ||
                         n.matches?.('.videoitem-in-speaking-3a-w-') ||
                         n.querySelector?.('.videoitem-in-speaking-3a-w-'))
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
                      t.querySelector?.('.active-speaker-halo') ||
                      t.matches?.('.videoitem-in-speaking-3a-w-') ||
                      t.classList?.contains?.('videoitem-in-speaking-3a-w-') ||
                      t.querySelector?.('.videoitem-in-speaking-3a-w-')
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
            await this.sendMessages(frame, details.startMessages, frameElement && frameElement.source === 'enterprise');
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
