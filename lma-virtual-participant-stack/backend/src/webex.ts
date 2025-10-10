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
        const messageElement = await frame.waitForSelector('.ql-editor[contenteditable="true"]');
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
        const joinFromBrowserButton = await page.waitForSelector('#broadcom-center-right', { timeout: 10000 });
        if (joinFromBrowserButton) {
            console.log('Found "Join from this browser" button, clicking it.');
            await joinFromBrowserButton.click();
        }
        console.log('Launching app.');
        const frameElement = await page.waitForSelector(this.iframe, { timeout: 15000 });
        const frame = await frameElement?.contentFrame();
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

        console.log("Opening chat panel.");
        try {
            await frame.waitForSelector('mdc-button[data-test="in-meeting-chat-toggle-button"]', {
                timeout: details.waitingTimeout,
            });
            await frame.click('mdc-button[data-test="in-meeting-chat-toggle-button"]');
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
        await this.sendMessages(frame, details.introMessages);

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
