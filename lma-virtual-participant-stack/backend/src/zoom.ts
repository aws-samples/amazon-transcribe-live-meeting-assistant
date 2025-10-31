import { Page,ConsoleMessage } from 'puppeteer';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';

export default class Zoom {
    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const messageElement = await page.waitForSelector(
            'p[data-placeholder="Type message here ..."]'
        );
        for (const message of messages) {
            await new Promise(resolve => setTimeout(resolve, 10));
            await messageElement?.type(message);
            await messageElement?.press('Enter');
        }
    }

    public async initialize(page: Page): Promise<void> {

        page.on('console', (message: ConsoleMessage) => {
            const type = message.type();
            const text = message.text();

            switch (type) {
                case 'log':
                    console.log(`Browser Log: ${text}`);
                    break; 
                case 'error':
                    console.error(`Browser Error: ${text}`);
                    break;
                case 'info':
                    console.info(`Browser Info: ${text}`);
                    break;
                default:
                    console.log(`Browser ${type}: ${text}`);
            }
        });

        // Add error handlers
        page.on('pageerror', (error: unknown) => {
            console.error('Page Error:', error);
        });

        page.on('error', (error: unknown) => {
            console.error('Browser Error:', error);
        });

        console.log('Getting Zoom meeting link.');
        await page.goto(`https://zoom.us/wc/${details.invite.meetingId}/join`);

        // Check for enterprise Zoom authentication requirement
        let enterpriseLogin = false;
        try {
            const authPrompt = await page.waitForSelector('#prompt', { timeout: 5000 });
            if (authPrompt) {
                const promptText = await page.evaluate(() => {
                    const promptDiv = document.querySelector('#prompt');
                    return promptDiv ? promptDiv.textContent : '';
                });
                
                if (promptText && promptText.includes('Sign in to join this meeting')) {
                    console.error('ERROR: Enterprise Zoom authentication required!');
                    console.error('The host requires authentication on the commercial Zoom platform.');
                    console.error('This meeting requires signing in with a commercial Zoom account.');
                    // throw new Error('Enterprise Zoom authentication required - cannot join without credentials');
                    enterpriseLogin = true;
                    // await new Promise(resolve => setTimeout(resolve, 500000));
                    await page.waitForSelector('.video-avatar__avatar', { timeout: 500000 });
                    await new Promise(resolve => setTimeout(resolve, 5000));
                }
            }
        } catch (error) {
            // If selector times out, continue normally (no auth required)
        }

        // if they logged in with enterprise they won't need to put in name password etc so skip
        if (enterpriseLogin === false) {
            // Handle meeting password if provided
            if (details.invite.meetingPassword) {
                console.log('Typing meeting password.');
                try {
                    const passwordTextElement = await page.waitForSelector('#input-for-pwd');
                    await passwordTextElement?.type(details.invite.meetingPassword);
                } catch (error) {
                    console.log('LMA Virtual Participant was unable to join the meeting.');
                    throw new Error('Meeting not found or invalid meeting ID');
                }
            }

            console.log('Clicking mute button.');
            const muteButton = await page.waitForSelector('svg.SvgAudioMute');
            await muteButton?.click();

            console.log('Clicking video button.');
            const stopVideoButton = await page.waitForSelector('svg.SvgVideoOn');
            await stopVideoButton?.click();

            console.log('Entering name.');
            try {
                const nameTextElement = await page.waitForSelector('#input-for-name');
                await nameTextElement?.type(details.scribeIdentity);
                await nameTextElement?.press('Enter');
            } catch (error) {
                console.log('LMA Virtual Participant was unable to join the meeting.');
                throw new Error('Meeting not found or invalid meeting ID');
            }
        }

        console.log('Waiting.');
        try {
            await page.waitForSelector('.video-avatar__avatar', {
                timeout: details.waitingTimeout,
            });
        } catch {
            console.log('LMA Virtual Participant was not admitted into the meeting.');
            return;
        }

        console.log('Opening chat panel.');
        const chatButtonElement = await page.waitForSelector(
            'button[aria-label="open the chat panel"]'
        );
        await chatButtonElement?.hover();
        await chatButtonElement?.click();

        console.log('Sending introduction messages.');
        await this.sendMessages(page, details.introMessages);

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
            const targetNode = document.querySelector('.footer-button__number-counter');
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
            let observer: MutationObserver | null = null;
            let viewSwitchAttempted = false;

            async function switchToSideBySideView() {
                if (viewSwitchAttempted) {
                    console.log('View switch already attempted, skipping');
                    return false;
                }
                console.log('Switching to side-by-side view');
                viewSwitchAttempted = true;
                
                // Wait longer for Zoom UI to be fully loaded
                await new Promise(resolve => setTimeout(resolve, 2000));

                console.log('Looking for view button...');
                const viewButton = document.querySelector('#full-screen-dropdown button') as HTMLElement;
                if (!viewButton) {
                    console.log('View button not found with selector: #full-screen-dropdown button');
                    return false;
                }
                
                console.log('Found view button, clicking...');
                viewButton.click();
                console.log('Clicked view button, waiting for options to appear');
                
                // Wait longer for dropdown to appear
                await new Promise(resolve => setTimeout(resolve, 2000));
                
                console.log('Looking for side-by-side option...');
                const sideBySideOption = document.querySelector('a[aria-label="Side-by-side: Speaker"]') as HTMLElement;
                if (!sideBySideOption) {
                    console.log('Side-by-side option not found with selector: a[aria-label="Side-by-side: Speaker"]');
                    return false;
                }
                
                console.log('Found side-by-side option, clicking...');
                sideBySideOption.click();
                console.log('Clicked side-by-side option');
                // Wait for view to change, then setup observer
                setTimeout(setupObserver, 3000);
                return true;
            }

            async function setupObserver() {
                // Disconnect existing observer if any
                if (observer) {
                    observer.disconnect();
                    observer = null;
                }

                // Find a stable parent element that's always present
                const parentNode = document.querySelector('.meeting-app');
                if (!parentNode) {
                    console.log('Parent container not found');
                    return;
                }

                // Check if speaker element exists
                const speakerElement = document.querySelector(
                    '.speaker-active-container__video-frame > .video-avatar__avatar > .video-avatar__avatar-footer'
                );

                if (!speakerElement) {
                    console.log('Speaker element not found');
                    if (!viewSwitchAttempted) {
                        switchToSideBySideView();
                    }
                    return;
                }

                const config = { 
                    childList: true, 
                    subtree: true 
                };

                const callback = (mutationList: MutationRecord[]) => {
                    const currentSpeakerElement = document.querySelector(
                        '.speaker-active-container__video-frame > .video-avatar__avatar > .video-avatar__avatar-footer'
                    );

                    if (currentSpeakerElement) {
                        const speakerName = currentSpeakerElement.textContent;
                        if (speakerName) {
                            console.log('Speaker detected:', speakerName);
                            (window as any).speakerChange(speakerName);
                        }
                    } else if (!viewSwitchAttempted) {
                        console.log('Speaker element lost, attempting to switch view');
                        switchToSideBySideView();
                    }
                };

                observer = new MutationObserver(callback);
                observer.observe(parentNode, config);

                // Handle initial state
                if (speakerElement.textContent) {
                    (window as any).speakerChange(speakerElement.textContent);
                }
            }
            setTimeout(() => {
                const initialSpeakerElement = document.querySelector(
                    '.speaker-active-container__video-frame > .video-avatar__avatar > .video-avatar__avatar-footer'
                );

                if (!initialSpeakerElement) {
                    console.log('Initial speaker element not found, attempting to switch view');
                    switchToSideBySideView();
                } else {
                    setupObserver();
                }
            }, 2000);
        });

        // Set up message monitoring with LMA features
        await page.exposeFunction('messageChange', async (message: string) => {
            if (message.includes(details.endCommand)) {
                console.log('LMA Virtual Participant has been removed from the meeting.');
                await this.sendMessages(page, details.exitMessages);
                details.start = false;
                await page.goto('about:blank');
            } else if (details.start && message.includes(details.pauseCommand)) {
                details.start = false;
                console.log(details.pauseMessages[0]);
                await this.sendMessages(page, details.pauseMessages);
            } else if (!details.start && message.includes(details.startCommand)) {
                details.start = true;
                console.log(details.startMessages[0]);
                await this.sendMessages(page, details.startMessages);
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
        await page.evaluate(() => {
            const targetNode = document.querySelector('div[aria-label="Chat Message List"]');
            const config = { childList: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                const addedNode = mutationList[mutationList.length - 1].addedNodes[0] as Element;
                if (addedNode) {
                    const message = addedNode
                        .querySelector('div[id^="chat-message-content"]')
                        ?.getAttribute('aria-label');
                    if (message && !message.startsWith('You to Everyone')) {
                        (window as any).messageChange(message);
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
            // Wait for either leave meeting button or meeting ended message
            const result = await Promise.race([
                // Only wait for URL change to about:blank (most reliable indicator)
                page.waitForFunction(
                    () => window.location.href === 'about:blank',
                    { timeout: 0 }
                ).then(() => 'URL_CHANGE_BLANK'),
                // Keep the 4-hour timeout as fallback
                page.waitForSelector(
                    'button.zm-btn.zm-btn-legacy.zm-btn--primary.zm-btn__outline--blue',
                    { timeout: details.meetingTimeout }
                ).then(() => 'LEGACY_BUTTON_TIMEOUT')
            ]);
            // console.log(`DEBUG: Meeting ended via: ${result}`);
            console.log('Meeting ended.');
        } catch (error) {
            // console.log(`DEBUG: Meeting timeout error: ${error instanceof Error ? error.message : String(error)}`);
            console.log('Meeting timed out.');
        } finally {
            details.start = false;
        }
    }
}
