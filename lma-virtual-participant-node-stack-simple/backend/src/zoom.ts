import { Page } from 'puppeteer';
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

    private async waitForMeetingUIReady(page: Page, timeout = 10000): Promise<void> {
        /**Wait for Zoom meeting UI to be ready by checking for multiple indicators*/
        try {
            await page.waitForSelector(
                '.zm-video-container, .meeting-client-view, .participants-counter',
                { timeout }
            );
        } catch (error) {
            console.log('Zoom meeting UI failed to load properly');
            throw error;
        }
    }

    public async initialize(page: Page): Promise<void> {
        console.log('Getting Zoom meeting link.');
        await page.goto(`https://zoom.us/wc/${details.invite.meetingId}/join`);

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

        await details.updateInvite('Joined');

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
            const targetNode = document.querySelector(
                '.speaker-active-container__video-frame > .video-avatar__avatar > .video-avatar__avatar-footer'
            );
            const config = { childList: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    const newSpeaker = mutation.target.textContent;
                    if (newSpeaker) (window as any).speakerChange(newSpeaker);
                }
            };
            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);

            const initialSpeaker = targetNode?.textContent;
            if (initialSpeaker) (window as any).speakerChange(initialSpeaker);
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
            await Promise.race([
                page.waitForSelector(
                    'button.leave-meeting-options__btn',
                    { timeout: 0 }
                ),
                page.waitForSelector(
                    'text="This meeting has been ended by host"',
                    { timeout: 0 }
                ),
                page.waitForSelector(
                    'button.zm-btn.zm-btn-legacy.zm-btn--primary.zm-btn__outline--blue',
                    { timeout: details.meetingTimeout }
                )
            ]);
            console.log('Meeting ended.');
        } catch (error) {
            console.log('Meeting timed out.');
        } finally {
            details.start = false;
            await details.updateInvite('Completed');
        }
    }
}
