/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page } from "puppeteer";
import { details } from "./details.js";
import { transcriptionService } from "./scribe.js";

export default class Zoom {
    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const messageElement = await page.waitForSelector(
            'p[data-placeholder="Type message here ..."]'
        );
        for (const message of messages) {
            await new Promise((resolve) => setTimeout(resolve, 10));
            await messageElement?.type(message);
            await messageElement?.press("Enter");
        }
    }

    public async initialize(page: Page): Promise<void> {
        console.log("Getting meeting link.");
        await page.goto(`https://zoom.us/wc/${details.invite.meetingId}/join`);

        if (details.invite.meetingPassword) {
            console.log("Typing meeting password.");
            const passwordTextElement = await page.waitForSelector("#input-for-pwd");
            await passwordTextElement?.type(details.invite.meetingPassword!);
        }

        console.log("Clicking mute button.");
        const muteButton = await page.waitForSelector("svg.SvgAudioMute");
        await muteButton?.click();

        console.log("Clicking video button.");
        const stopVideoButton = await page.waitForSelector("svg.SvgVideoOn");
        await stopVideoButton?.click();

        console.log("Entering name.");
        try {
            const nameTextElement = await page.waitForSelector("#input-for-name");
            await nameTextElement?.type(details.scribeIdentity);
            await nameTextElement?.press("Enter");
        } catch {
            console.log("Your scribe was unable to join the meeting.");
            return;
        }

        console.log("Waiting.");
        try {
            await page.waitForSelector(".video-avatar__avatar", {
                timeout: details.waitingTimeout,
            });
        } catch {
            console.log("Your scribe was not admitted into the meeting.");
            return;
        }

        console.log("Opening chat panel.");
        const chatButtonElement = await page.waitForSelector(
            'button[aria-label="open the chat panel"]'
        );
        await chatButtonElement?.hover();
        await chatButtonElement?.click();

        details.updateInvite("Joined");
        console.log("Sending introduction messages.");
        await this.sendMessages(page, details.introMessages);

        await page.exposeFunction("attendeeChange", async (number: number) => {
            if (number <= 1) {
                console.log("Your scribe got lonely and left.");
                details.start = false;
                await page.browser().close();
            }
        });
        console.log("Listening for attendee changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector(".footer-button__number-counter");
            const config = { characterData: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                const number = parseInt(
                    mutationList[mutationList.length - 1].target.textContent || "0"
                );
                (window as any).attendeeChange(number);
            };
            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);
        });

        await page.exposeFunction("speakerChange", transcriptionService.speakerChange);
        console.log("Listening for speaker changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector(
                ".speaker-active-container__video-frame > .video-avatar__avatar > .video-avatar__avatar-footer"
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

        await page.exposeFunction("messageChange", async (message: string) => {
            if (message.includes(details.endCommand)) {
                console.log("Your scribe has been removed from the meeting.");
                await page.browser().close();
            } else if (details.start && message.includes(details.pauseCommand)) {
                details.start = false;
                console.log(details.pauseMessages[0]);
                await this.sendMessages(page, details.pauseMessages);
            } else if (!details.start && message.includes(details.startCommand)) {
                details.start = true;
                console.log(details.startMessages[0]);
                await this.sendMessages(page, details.startMessages);
            } else if (details.start) {
                details.messages.push(message);
            }
        });
        console.log("Listening for message changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector('div[aria-label="Chat Message List"]');
            const config = { childList: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                const addedNode = mutationList[mutationList.length - 1].addedNodes[0] as Element;
                if (addedNode) {
                    const message = addedNode
                        .querySelector('div[id^="chat-message-content"]')
                        ?.getAttribute("aria-label");
                    if (message && !message.startsWith("You to Everyone")) {
                        (window as any).messageChange(message);
                    }
                }
            };
            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);
        });

        console.log("Waiting for meeting end.");
        try {
            await page.waitForSelector(
                "button.zm-btn.zm-btn-legacy.zm-btn--primary.zm-btn__outline--blue",
                {
                    timeout: details.meetingTimeout,
                }
            );
            console.log("Meeting ended.");
        } catch {
            console.log("Meeting timed out.");
        } finally {
            details.start = false;
        }
    }
}
