/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page } from "puppeteer";
import { details } from "./details.js";
import { transcriptionService } from "./scribe.js";

export default class Chime {
    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const messageElement = await page.waitForSelector(
            'textarea[placeholder="Message all attendees"]'
        );
        for (const message of messages) {
            await messageElement?.type(message);
            await messageElement?.press("Enter");
        }
    }

    private prevSender: string = "";

    public async initialize(page: Page): Promise<void> {
        console.log("Getting meeting link.");
        await page.goto(`https://app.chime.aws/meetings/${details.invite.meetingId}`);

        console.log("Entering name.");
        try {
            const nameTextElement = await page.waitForSelector("#name");
            await nameTextElement?.type(details.scribeIdentity);
            await nameTextElement?.press("Tab");
            await page.keyboard.press("Enter");
        } catch {
            console.log("Your scribe was unable to join the meeting.");
            return;
        }

        console.log("Clicking mute button.");
        const muteCheckboxElement = await page.waitForSelector("::-p-text(Join muted)");
        await muteCheckboxElement?.click();

        console.log("Clicking join button.");
        const joinButtonElement = await page.waitForSelector(
            'button[data-testid="button"][aria-label="Join"]'
        );
        await joinButtonElement?.click();

        console.log("Opening chat panel.");
        try {
            const chatPanelElement = await page.waitForSelector(
                'button[data-testid="button"][aria-label^="Open chat panel"]',
                { timeout: details.waitingTimeout }
            );
            await chatPanelElement?.click();
        } catch {
            console.log("Your scribe was not admitted into the meeting.");
            return;
        }

        await new Promise((resolve) => setTimeout(resolve, 1000));

        details.updateInvite("Joined");
        console.log("Sending introduction messages.");
        await this.sendMessages(page, details.introMessages);

        console.log("Opening attendees panel.");
        const attendeesPanelElement = await page.waitForSelector(
            'button[data-testid="button"][aria-label^="Open attendees panel"]'
        );
        await attendeesPanelElement?.click();

        await page.exposeFunction("attendeeChange", (number: number) => {
            if (number <= 1) {
                console.log("Your scribe got lonely and left.");
                page.goto("about:blank");
            }
        });
        console.log("Listening for attendee changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector(
                'button[data-testid="collapse-container"][aria-label^="Present"]'
            );
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
                ".activeSpeakerCell ._3yg3rB2Xb_sfSzRXkm8QT-"
            );

            if (targetNode) {
                const initialSpeaker = targetNode.textContent;
                if (initialSpeaker !== "No one") {
                    (window as any).speakerChange(initialSpeaker);
                }
            }

            const config = { characterData: true, subtree: true };

            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    const newSpeaker = mutation.target.textContent;
                    if (newSpeaker !== "No one") {
                        (window as any).speakerChange(newSpeaker);
                    }
                }
            };

            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);
        });

        await page.exposeFunction(
            "messageChange",
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

                if (text === details.endCommand) {
                    console.log("Your scribe has been removed from the meeting.");
                    await page.goto("about:blank");
                } else if (details.start && text === details.pauseCommand) {
                    details.start = false;
                    console.log(details.pauseMessages[0]);
                    await this.sendMessages(page, details.pauseMessages);
                } else if (!details.start && text === details.startCommand) {
                    details.start = true;
                    console.log(details.startMessages[0]);
                    await this.sendMessages(page, details.startMessages);
                } else if (
                    details.start &&
                    sender !== "Amazon Chime" &&
                    !sender?.includes(details.scribeName)
                ) {
                    const timestamp = new Date().toLocaleTimeString("en-US", {
                        hour12: false,
                        hour: "2-digit",
                        minute: "2-digit",
                    });
                    let message = `[${timestamp}] ${sender}: `;

                    if (attachmentTitle && attachmentHref) {
                        details.attachments[attachmentTitle] = attachmentHref;
                        message += text ? `${text} | ${attachmentTitle}` : attachmentTitle;
                    } else if (text) {
                        message += text;
                    }

                    details.messages.push(message);
                }
            }
        );
        console.log("Listening for message changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector("._2B9DdDvc2PdUbvEGXfOU20");
            const config = { childList: true, subtree: true };

            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    const addedNode = mutation.addedNodes[0] as Element;
                    if (addedNode) {
                        const sender = addedNode.querySelector(
                            'h3[data-testid="chat-bubble-sender-name"]'
                        )?.textContent;
                        const text = addedNode.querySelector(".Linkify")?.textContent;
                        const attachmentElement = addedNode.querySelector(
                            ".SLFfm3Dwo5MfFzks4uM11"
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

        console.log("Waiting for meeting end.");
        try {
            await page.waitForSelector('button[id="endMeeting"]', {
                hidden: true,
                timeout: details.meetingTimeout,
            });
            console.log("Meeting ended.");
        } catch {
            console.log("Meeting timed out.");
        } finally {
            details.start = false;
        }
    }
}
