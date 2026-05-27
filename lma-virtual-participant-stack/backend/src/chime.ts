import { Page } from 'rebrowser-puppeteer';
import { details, matchesEndCommand, exitMessagesFor } from './details.js';
import { transcriptionService } from './scribe.js';
import { voiceAssistant } from './voice-assistant.js';
import { findElementWithFallback } from './ai-dom-resolver.js';
import { startDialogWatchdog } from './dialog-watchdog.js';

export default class Chime {
    private prevSender: string = '';

    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const found = await findElementWithFallback(
            page,
            ['textarea[placeholder="Message all attendees"]'],
            {
                intent: 'Chime in-meeting chat panel message compose textarea',
                platform: 'CHIME',
                step: 'chime.chat.input',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!found) {
            console.log('Could not locate Chime chat input — aborting sendMessages');
            return;
        }
        for (const message of messages) {
            await found.element.type(message);
            await found.element.press('Enter');
            await new Promise(resolve => setTimeout(resolve, 100));
        }
    }

    public async initialize(page: Page): Promise<void> {
        // AI-driven dialog watchdog runs for the entire meeting lifecycle.
        // See dialog-watchdog.ts. Catches sign-in / pre-join / waiting-room /
        // in-meeting dialogs (consent, recording notice, etc.) and either
        // auto-dismisses (CONSENT-class) or escalates to MANUAL_ACTION_REQUIRED
        // so the user can clear it via VNC.
        startDialogWatchdog(page, { platform: 'CHIME' });

        console.log('Getting Chime meeting link.');
        await page.goto(`https://app.chime.aws/meetings/${details.invite.meetingId}`);

        console.log('Entering name.');
        const nameRes = await findElementWithFallback(
            page,
            ['#name'],
            {
                intent: 'Chime pre-join screen display-name input field',
                platform: 'CHIME',
                step: 'chime.join.name',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!nameRes) {
            console.log('LMA Virtual Participant was unable to join the meeting.');
            throw new Error('Meeting not found or invalid meeting ID');
        }
        await nameRes.element.type(details.scribeIdentity, { delay: 100 });
        await nameRes.element.press('Tab');
        await page.keyboard.press('Enter');

        // Only click mute button if voice assistant is NOT enabled
        if (!voiceAssistant.isEnabled()) {
            console.log('Clicking mute button.');
            const muteRes = await findElementWithFallback(
                page,
                ['text/Join muted'],
                {
                    intent: 'Chime pre-join "Join muted" checkbox/control',
                    platform: 'CHIME',
                    step: 'chime.join.muteToggle',
                },
                { maxRetries: 10, delayMs: 500 },
            );
            await muteRes?.element.click();
        } else {
            console.log('Voice assistant enabled - skipping mute button for agent audio');
        }

        console.log('Clicking join button.');
        const joinRes = await findElementWithFallback(
            page,
            ['button[data-testid="button"][aria-label="Join"]'],
            {
                intent: 'Chime pre-join primary "Join" button',
                platform: 'CHIME',
                step: 'chime.join.joinButton',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!joinRes) {
            console.log('Could not locate Chime Join button — aborting');
            return;
        }
        await joinRes.element.click();

        console.log('Opening chat panel.');
        try {
            const chatPanelElement = await page.waitForSelector(
                'button[data-testid="button"][aria-label^="Open chat panel"]',
                { timeout: details.waitingTimeout }
            );
            await chatPanelElement?.click();
        } catch (error) {
            console.log('LMA Virtual Participant was not admitted into the meeting.');
            throw new Error('Wrong meeting password or permission denied');
        }

        await new Promise(resolve => setTimeout(resolve, 1000));

        console.log('Successfully joined Chime meeting');

        console.log('Sending introduction messages.');
        await this.sendMessages(page, details.introMessages);

        console.log('Opening attendees panel.');
        const attendeesPanelElement = await page.waitForSelector(
            'button[data-testid="button"][aria-label^="Open attendees panel"]'
        );
        await attendeesPanelElement?.click();

        // Set up attendee change monitoring
        await page.exposeFunction('attendeeChange', async (number: number) => {
            if (number <= 1) {
                console.log('LMA Virtual Participant got lonely and left.');
                details.start = false;
                await page.goto('about:blank');
            }
        });

        console.log('Listening for attendee changes.');
        await page.evaluate(() => {
            const targetNode = document.querySelector(
                'button[data-testid="collapse-container"][aria-label^="Present"]'
            );
            const config = { characterData: true, subtree: true };

            const callback = (mutationList: MutationRecord[]) => {
                const number = parseInt(
                    mutationList[mutationList.length - 1].target.textContent || '0'
                );
                (window as any).attendeeChange(number);
            };

            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);
        });

        // Set up speaker change monitoring
        await page.exposeFunction('speakerChange', async (speaker: string) => {
            await transcriptionService.speakerChange(speaker);
        });

        console.log('Listening for speaker changes.');
        await page.evaluate(() => {
            const targetNode = document.querySelector(
                '.activeSpeakerCell ._3yg3rB2Xb_sfSzRXkm8QT-'
            );

            if (targetNode) {
                const initialSpeaker = targetNode.textContent;
                if (initialSpeaker !== 'No one') {
                    (window as any).speakerChange(initialSpeaker);
                }
            }

            const config = { characterData: true, subtree: true };

            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    const newSpeaker = mutation.target.textContent;
                    if (newSpeaker !== 'No one') {
                        (window as any).speakerChange(newSpeaker);
                    }
                }
            };

            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);
        });

        // Set up message monitoring with LMA features
        await page.exposeFunction(
            'messageChange',
            async (
                sender: string | null,
                text: string | null,
                attachmentTitle: string | null,
                attachmentHref: string | null
            ) => {
                if (!sender) {
                    sender = this.prevSender;
                }
                this.prevSender = sender;

                // Handle LMA commands
                if (text && matchesEndCommand(text)) {
                    console.log(`LMA Virtual Participant has been asked to leave by ${sender || 'a participant'}.`);
                    await this.sendMessages(page, exitMessagesFor(sender));
                    details.start = false;
                    await page.goto('about:blank');
                } else if (details.start && text === details.pauseCommand) {
                    details.start = false;
                    console.log(details.pauseMessages[0]);
                    await this.sendMessages(page, details.pauseMessages);
                } else if (!details.start && text === details.startCommand) {
                    details.start = true;
                    console.log(details.startMessages[0]);
                    await this.sendMessages(page, details.startMessages);
                    // Restart transcription if needed
                    transcriptionService.startTranscription();
                } else if (
                    details.start &&
                    sender !== 'Amazon Chime' &&
                    !sender?.includes(details.scribeName)
                ) {
                    const timestamp = new Date().toLocaleTimeString('en-US', {
                        hour12: false,
                        hour: '2-digit',
                        minute: '2-digit',
                    });
                    let message = `[${timestamp}] ${sender}: `;

                    if (attachmentTitle && attachmentHref) {
                        details.attachments[attachmentTitle] = attachmentHref;
                        message += text
                            ? `${text} | ${attachmentTitle}`
                            : attachmentTitle;
                    } else if (text) {
                        message += text;
                    }

                    details.messages.push(message);
                    console.log('New message:', message);
                }
            }
        );

        console.log('Listening for message changes.');
        await page.evaluate(() => {
            const targetNode = document.querySelector('._2B9DdDvc2PdUbvEGXfOU20');
            const config = { childList: true, subtree: true };

            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    const addedNode = mutation.addedNodes[0] as Element;
                    if (addedNode) {
                        const sender = addedNode.querySelector(
                            'h3[data-testid="chat-bubble-sender-name"]'
                        )?.textContent;
                        const text = addedNode.querySelector('.Linkify')?.textContent;
                        const attachmentElement = addedNode.querySelector(
                            '.SLFfm3Dwo5MfFzks4uM11'
                        ) as HTMLAnchorElement;
                        const attachmentTitle = attachmentElement?.title;
                        const attachmentHref = attachmentElement?.href;
                        (window as any).messageChange(
                            sender,
                            text,
                            attachmentTitle,
                            attachmentHref
                        );
                    }
                }
            };

            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);
        });

        // Start transcription if enabled (LMA behavior)
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(page, details.startMessages);
            transcriptionService.startTranscription();
        }

        console.log('Waiting for meeting end.');
        try {
            await new Promise((resolve, reject) => {
                const checkInterval = setInterval(async () => {
                    try {
                        // Check for "Your meeting has ended" text
                        const meetingEndedElement = await page.$('text/Your meeting has ended');
                        if (meetingEndedElement) {
                            console.log('Meeting ended: "Your meeting has ended" text detected');
                            clearInterval(checkInterval);
                            resolve(undefined);
                            return;
                        }
                        
                        // Fallback: Check if page navigated away
                        const currentUrl = await page.url();
                        if (currentUrl === 'about:blank' || !currentUrl.includes('chime.aws')) {
                            console.log('Meeting ended: Page navigated away');
                            clearInterval(checkInterval);
                            resolve(undefined);
                            return;
                        }
                        
                    } catch (error) {
                        clearInterval(checkInterval);
                        reject(error);
                    }
                }, 2000); // Check every 2 seconds for better responsiveness
                
                // Set up timeout
                setTimeout(() => {
                    clearInterval(checkInterval);
                    reject(new Error('Meeting timeout'));
                }, details.meetingTimeout);
            });
            console.log("Meeting ended.");
        } catch (error) {
            console.log("Meeting timed out.");
        } finally {
            details.start = false;
        }
    }
}
