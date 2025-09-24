/* eslint-disable @typescript-eslint/no-explicit-any */
// import { BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";
import { Page } from "puppeteer";
import { details } from "./details.js";
import { transcriptionService } from "./scribe.js";
import { createStatusManager } from "./status-manager.js";

// const bedrockClient = new BedrockRuntimeClient();

export default class Teams {
    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const messageElement = await page.waitForSelector(".ck-placeholder");
        for (const message of messages) {
            await messageElement?.click();
            await new Promise((resolve) => setTimeout(resolve, 500));
            await messageElement?.type(message);
            await messageElement?.press("Enter");
        }
    }

    public async initialize(page: Page): Promise<void> {
        try {
            console.log("Getting meeting link.");
            await page.goto(
                `https://teams.live.com/v2/?meetingjoin=true#/meet/${details.invite.meetingId}?launchAgent=marketing_join&laentry=hero&p=${details.invite.meetingPassword}&anon=true`
            );
        } catch {
            console.log("Your scribe was unable to join the meeting.");
            return;
        }

        console.log("Entering name.");
        const nameTextElement = await page.waitForSelector(
            '[data-tid="prejoin-display-name-input"]'
        );
        await nameTextElement?.type(details.scribeIdentity, { delay: 100 });
        await nameTextElement?.press("Enter");

        await new Promise((resolve) => setTimeout(resolve, 250));
        console.log("Clicking mute button.");
        const muteButtonElement = await page.waitForSelector('[data-tid="toggle-mute"]');
        await muteButtonElement?.click();

        await new Promise((resolve) => setTimeout(resolve, 250));
        console.log("Clicking video button.");
        const videoButtonElement = await page.waitForSelector('[data-tid="toggle-video"]');
        await videoButtonElement?.click();

        await new Promise((resolve) => setTimeout(resolve, 250));
        console.log("Clicking join button.");
        const joinButtonElement = await page.waitForSelector('[data-tid="prejoin-join-button"]');
        await joinButtonElement?.click();

        // try {
        //     const captchaImageElement = await page.waitForSelector(
        //         '[data-tid="HIP-Captcha-Image"]',
        //         {
        //             timeout: 5000,
        //         }
        //     );
        //     await new Promise((resolve) => setTimeout(resolve, 250));
        //     console.log("Solving captcha.");
        //     const captchaScreenshot = await captchaImageElement!.screenshot({ encoding: "base64" });

        //     // const response = await bedrockClient.send(
        //     //     new ConverseCommand({
        //     //         modelId: process.env.MODEL_ID!,
        //     //         system: [
        //     //             {
        //     //                 text: `You are a meticulous assistant that deeply analyzes a captcha image and returns its exact characters left to right, top to bottom.
        //     //                 You take your time and do your absolute best to not confuse numbers and letters.`,
        //     //             },
        //     //         ],
        //     //         messages: [
        //     //             {
        //     //                 role: "user",
        //     //                 content: [
        //     //                     {
        //     //                         image: {
        //     //                             format: "png",
        //     //                             source: {
        //     //                                 bytes: Buffer.from(captchaScreenshot, "base64"),
        //     //                             },
        //     //                         },
        //     //                     },
        //     //                     {
        //     //                         text: "What characters are in this captcha image? Respond with only the characters, no other text.",
        //     //                     },
        //     //                 ],
        //     //             },
        //     //         ],
        //     //         inferenceConfig: {
        //     //             maxTokens: 50,
        //     //             temperature: 0.1,
        //     //         },
        //     //     })
        //     // );
        //     // const captchaText = response.output?.message?.content?.[0]?.text?.trim();
        //     // const captchaInputElement = await page.waitForSelector(
        //     //     '[data-tid="HIP-Captcha-Input"]'
        //     // );
        //     // await captchaInputElement?.type(captchaText || "rip", { delay: 250 });
        //     // await captchaInputElement?.press("Enter");
        //     try {
        //         await page.waitForSelector('[data-tid="calling-retry-rejoinbutton"]', {
        //             timeout: 3000,
        //         });
        //         console.log("Your scribe failed to solve the captcha.");
        //         return;
        //     } catch {}
        // } catch {
        //     console.log("No captcha found.");
        // }

        console.log("Opening chat panel.");
        try {
            const chatPanelElement = await page.waitForSelector("#chat-button", {
                timeout: details.waitingTimeout,
                visible: true,
            });
            await chatPanelElement?.click();
        } catch {
            console.log("Your scribe was not admitted into the meeting.");
            return;
        }

        // Update status to JOINED
        if (details.invite.virtualParticipantId) {
            const statusManager = createStatusManager(details.invite.virtualParticipantId);
            await statusManager.setJoined();
        }
        console.log("Sending introduction messages.");
        await this.sendMessages(page, details.introMessages);

        console.log("Opening view panel.");
        const viewPanelElement = await page.waitForSelector("#custom-view-button", {
            timeout: details.waitingTimeout,
        });
        await viewPanelElement?.click();

        console.log("Selecting speaker view.");
        const speakerViewElement = await page.waitForSelector("#SpeakerView-button", {
            timeout: details.waitingTimeout,
        });
        await speakerViewElement?.click();

        // Set up simple attendee change monitoring
        await page.exposeFunction('attendeeChange', async (hasOthers: boolean) => {
            console.log(`DEBUG: Teams has other participants: ${hasOthers}`);
            if (!hasOthers) {
                console.log('LMA Virtual Participant got lonely and left.');
                details.start = false;
                await page.goto('about:blank');
            }
        });

        console.log("Listening for attendee changes.");
        await page.evaluate(() => {
            const checkAttendeeCount = () => {
                const badgeElement = document.querySelector('span[data-tid="toolbar-item-badge"]');
                const hasOthers = badgeElement && parseInt(badgeElement.textContent || '0') > 1;
                console.log(`DEBUG: Badge element found: ${!!badgeElement}, count: ${badgeElement?.textContent || 'N/A'}, hasOthers: ${hasOthers}`);
                (window as any).attendeeChange(hasOthers);
            };

            // Check initial state
            checkAttendeeCount();

            // Monitor for badge appearance/disappearance
            const rosterButton = document.querySelector('#roster-button, button[data-inp="roster-button"]');
            if (rosterButton) {
                const config = { childList: true, subtree: true, characterData: true };
                const callback = () => {
                    checkAttendeeCount();
                };
                const observer = new MutationObserver(callback);
                observer.observe(rosterButton, config);
                console.log('DEBUG: Teams attendee monitoring set up on roster button');
            } else {
                console.log('DEBUG: Teams roster button not found - attendee monitoring disabled');
            }
        });

        await page.exposeFunction("speakerChange", transcriptionService.speakerChange);
        console.log("Listening for speaker changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector('[data-tid="SpeakerStage-wrapper"]');
            const config = { characterData: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    const newSpeaker = mutation.target.textContent;
                    if (newSpeaker) {
                        (window as any).speakerChange(newSpeaker);
                    }
                }
            };
            const observer = new MutationObserver(callback);
            if (targetNode) observer.observe(targetNode, config);

            const initialSpeaker = targetNode?.textContent;
            if (initialSpeaker) {
                (window as any).speakerChange(initialSpeaker);
            }
        });

        await page.exposeFunction("messageChange", async (message: string) => {
            if (message.includes(details.endCommand)) {
                console.log("Your scribe has been removed from the meeting.");
                await page.browser().close();
            } else if (
                details.start &&
                message.includes(details.pauseCommand) &&
                !message.includes(`"${details.pauseCommand}"`)
            ) {
                details.start = false;
                console.log(details.pauseMessages[0]);
                await this.sendMessages(page, details.pauseMessages);
            } else if (
                !details.start &&
                message.includes(details.startCommand) &&
                !message.includes(`"${details.startCommand}"`)
            ) {
                details.start = true;
                console.log(details.startMessages[0]);
                await this.sendMessages(page, details.startMessages);
            } else if (details.start) {
                details.messages.push(message);
            }
        });
        await new Promise((resolve) => setTimeout(resolve, 1000));
        console.log("Listening for message changes.");
        await page.evaluate(() => {
            const targetNode = document.querySelector("#chat-pane-list");
            const config = { childList: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    for (const addedNode of mutation.addedNodes) {
                        if (addedNode.nodeType === Node.ELEMENT_NODE) {
                            const element = addedNode as Element;
                            const messageElement = element.querySelector(
                                'div[dir="auto"][role="heading"][aria-level="4"]'
                            );
                            if (messageElement) {
                                (window as any).messageChange(messageElement.textContent);
                            }
                        }
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

        console.log("Waiting for meeting end.");
        try {
            // Wait for multiple Teams meeting end indicators
            const result = await Promise.race([
                // Wait for hangup button to be hidden (original logic)
                page.waitForSelector("#hangup-button", {
                    hidden: true,
                    timeout: details.meetingTimeout,
                }).then(() => 'HANGUP_BUTTON_HIDDEN'),
                // Wait for rejoin button to appear (when meeting ends)
                page.waitForSelector('button[data-tid="anon-meeting-end-screen-rejoin-button"]', {
                    timeout: details.meetingTimeout,
                }).then(() => 'REJOIN_BUTTON_APPEARED'),
                // Monitor for URL change to about:blank
                page.waitForFunction(
                    () => window.location.href === 'about:blank',
                    { timeout: details.meetingTimeout }
                ).then(() => 'URL_CHANGE_BLANK')
            ]);
            console.log(`DEBUG: Teams meeting ended via: ${result}`);
            console.log("Meeting ended.");
        } catch (error) {
            console.log(`DEBUG: Teams meeting timeout error: ${error instanceof Error ? error.message : String(error)}`);
            console.log("Meeting timed out.");
        } finally {
            details.start = false;
            // Update status to COMPLETED
            if (details.invite.virtualParticipantId) {
                const statusManager = createStatusManager(details.invite.virtualParticipantId);
                await statusManager.setCompleted();
            }
        }
    }
}
