import { Page, Frame } from 'puppeteer';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';

export default class Webex {
    private readonly iframe = 'iframe[name="thinIframe"]';

    private async sendMessages(
        frame: Frame,
        messages: string[]
    ): Promise<void> {
        const messageElement = await frame.waitForSelector(
            'textarea[placeholder="Type your message here"]'
        );
        for (const message of messages) {
            await messageElement?.type(message);
            await messageElement?.press('Enter');
        }
    }

    public async initialize(page: Page): Promise<void> {
        console.log('Getting Webex meeting link.');
        await page.goto('https://signin.webex.com/join');

        console.log('Entering meeting ID.');
        const meetingTextElement = await page.waitForSelector(
            '#join-meeting-form'
        );
        await meetingTextElement?.type(details.invite.meetingId);
        await meetingTextElement?.press('Enter');

        console.log('Launching app.');
        try {
            await page.waitForSelector('.meet_message_H1');
            await page.goto(`${page.url()}?launchApp=true`);
        } catch (error) {
            console.log('LMA Virtual Participant was unable to join the meeting.');
            throw new Error('Meeting not found or invalid meeting ID');
        }

        const frameElement = await page.waitForSelector(this.iframe);
        const frame = await frameElement?.contentFrame();
        if (!frame) {
            throw new Error('Failed to access Webex meeting frame');
        }

        console.log('Entering name.');
        const nameTextElement = await frame.waitForSelector(
            'input[aria-labelledby="nameLabel"]'
        );
        await nameTextElement?.type(details.scribeIdentity);

        console.log('Entering email.');
        const emailTextElement = await frame.waitForSelector(
            'input[aria-labelledby="emailLabel"]'
        );
        // Use a generic bot email for Webex
        await emailTextElement?.type('lma-bot@example.com');
        await emailTextElement?.press('Enter');

        console.log('Clicking cookie button.');
        try {
            const cookieButtonElement = await page.waitForSelector(
                '.cookie-manage-close-handler',
                { timeout: 2000 }
            );
            await cookieButtonElement?.click();
        } catch {
            // Cookie button may not appear
        }

        console.log('Clicking mute button.');
        const muteButtonElement = await frame.waitForSelector('text="Mute"');
        await muteButtonElement?.click();

        console.log('Clicking video button.');
        const videoButtonElement = await frame.waitForSelector(
            'text="Stop video"'
        );
        await videoButtonElement?.click();

        console.log('Clicking join button.');
        const joinButtonElement = await frame.waitForSelector(
            'text="Join meeting"'
        );
        await joinButtonElement?.click();

        console.log('Opening chat panel.');
        try {
            const chatPanelElement = await frame.waitForSelector(
                'text="Chat"',
                { timeout: details.waitingTimeout }
            );
            await chatPanelElement?.click();
        } catch (error) {
            console.log('LMA Virtual Participant was not admitted into the meeting.');
            throw new Error('Wrong meeting password or permission denied');
        }

        await new Promise(resolve => setTimeout(resolve, 1000));

        // Update status to JOINED
        await details.updateInvite('Joined');
        console.log('Successfully joined Webex meeting');

        console.log('Sending introduction messages.');
        await this.sendMessages(frame, details.introMessages);

        // Set up speaker change monitoring
        await page.exposeFunction('speakerChange', async (speaker: string) => {
            await transcriptionService.speakerChange(speaker);
        });

        console.log('Listening for speaker changes.');
        await page.evaluate(
            ({ iframe }) => {
                const iFrame = document.querySelector(
                    iframe
                ) as HTMLIFrameElement;
                const iFrameDocument = iFrame?.contentDocument;
                const targetNode = iFrameDocument?.querySelector(
                    'div[class*="layout-layout-content-left"]'
                );

                const config = { attributes: true, subtree: true };

                const callback = (mutationList: MutationRecord[]) => {
                    for (const mutation of mutationList) {
                        if (mutation.attributeName === 'class') {
                            const childNode = mutation.target as HTMLElement;
                            const pattern = /.*videoitem-in-speaking.*/;
                            if (childNode.classList.value.match(pattern)) {
                                const nameElement = childNode.querySelector(
                                    '[class^="videoitem-full-name-content"]'
                                );
                                if (nameElement) {
                                    (window as any).speakerChange(
                                        nameElement.textContent
                                    );
                                }
                            }
                        }
                    }
                };

                const observer = new MutationObserver(callback);
                if (targetNode) observer.observe(targetNode, config);
            },
            { iframe: this.iframe }
        );

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
        await page.evaluate(
            ({ iframe }) => {
                const iFrame = document.querySelector(
                    iframe
                ) as HTMLIFrameElement;
                const iFrameDocument = iFrame?.contentDocument;
                const targetNode = iFrameDocument?.querySelector(
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
            },
            { iframe: this.iframe }
        );

        // Start transcription if enabled (LMA behavior)
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(frame, details.startMessages);
            transcriptionService.startTranscription();
        }

        console.log('Waiting for meeting end.');
        try {
            await frame.waitForSelector('.style-end-message-2PkYs', {
                timeout: details.meetingTimeout,
            });
            console.log('Meeting ended.');
        } catch (error) {
            console.log('Meeting timed out.');
        } finally {
            details.start = false;
            await details.updateInvite('Completed');
        }
    }
}
