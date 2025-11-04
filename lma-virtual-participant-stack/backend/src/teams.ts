/* eslint-disable @typescript-eslint/no-explicit-any */
// import { BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";
import { Page } from "puppeteer";
import { details } from "./details.js";
import { transcriptionService } from "./scribe.js";
import { createStatusManager } from "./status-manager.js";

// const bedrockClient = new BedrockRuntimeClient();

export default class Teams {
    // Helper function to generate random delay between min and max milliseconds
    private randomDelay(min: number, max: number): number {
        return Math.floor(Math.random() * (max - min + 1)) + min;
    }

    // Helper function to wait with random delay
    private async waitRandom(min: number, max: number): Promise<void> {
        const delay = this.randomDelay(min, max);
        await new Promise((resolve) => setTimeout(resolve, delay));
    }

    // Helper function to simulate human-like mouse movement before clicking
    private async humanClick(page: Page, selector: string): Promise<void> {
        const element = await page.waitForSelector(selector);
        if (!element) return;

        // Get element position
        const box = await element.boundingBox();
        if (box) {
            // Move mouse to a random point within the element
            const x = box.x + this.randomDelay(10, box.width - 10);
            const y = box.y + this.randomDelay(10, box.height - 10);
            
            // Move mouse in a slightly curved path
            await page.mouse.move(x - 50, y - 50, { steps: this.randomDelay(5, 10) });
            await this.waitRandom(50, 150);
            await page.mouse.move(x, y, { steps: this.randomDelay(5, 10) });
            await this.waitRandom(100, 300);
        }
        
        await element.click();
    }

    // Helper function to type with human-like delays
    private async humanType(page: Page, selector: string, text: string): Promise<void> {
        const element = await page.waitForSelector(selector);
        if (!element) return;

        await element.click();
        await this.waitRandom(200, 500);

        // Type each character with random delays
        for (const char of text) {
            await element.type(char, { delay: this.randomDelay(80, 200) });
            // Occasionally pause longer (simulating thinking)
            if (Math.random() < 0.1) {
                await this.waitRandom(300, 800);
            }
        }
    }

    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const messageElement = await page.waitForSelector(".ck-placeholder");
        for (const message of messages) {
            await messageElement?.click();
            await this.waitRandom(400, 800);
            
            // Type message with human-like delays
            for (const char of message) {
                await messageElement?.type(char, { delay: this.randomDelay(80, 200) });
                if (Math.random() < 0.1) {
                    await this.waitRandom(200, 500);
                }
            }
            
            await this.waitRandom(300, 600);
            await messageElement?.press("Enter");
            await this.waitRandom(500, 1000);
        }
    }

    public async initialize(page: Page): Promise<void> {
        try {
            console.log("Getting meeting link.");
            await page.goto(
                `https://teams.microsoft.com/v2/?meetingjoin=true#/meet/${details.invite.meetingId}?p=${details.invite.meetingPassword}&anon=true`
            );
        } catch {
            console.log("Your scribe was unable to join the meeting.");
            return;
        }

        console.log("Entering name.");
        await this.waitRandom(500, 1000); // Pause before starting to type
        await this.humanType(page, '[data-tid="prejoin-display-name-input"]', details.scribeIdentity);
        await this.waitRandom(400, 800);
        
        const nameTextElement = await page.waitForSelector('[data-tid="prejoin-display-name-input"]');
        await nameTextElement?.press("Enter");

        await this.waitRandom(600, 1200);
        console.log("Clicking mute button.");
        await this.humanClick(page, '[data-tid="toggle-mute"]');

        await this.waitRandom(500, 1000);
        console.log("Clicking video button.");
        await this.humanClick(page, '[data-tid="toggle-video"]');

        await this.waitRandom(800, 1500);
        console.log("Clicking join button.");
        await this.humanClick(page, '[data-tid="prejoin-join-button"]');

        // Wait for potential CAPTCHA with longer timeout
        console.log("Checking for CAPTCHA...");
        await this.waitRandom(2000, 4000);
        
        try {
            const captchaElement = await page.waitForSelector(
                '[data-tid="HIP-Captcha-Image"]',
                { timeout: 5000 }
            );
            
            if (captchaElement) {
                console.log("CAPTCHA detected! Waiting for manual resolution...");
                console.log("Please solve the CAPTCHA in the VNC viewer.");
                
                // Wait for CAPTCHA to be solved (join button to disappear or chat to appear)
                await Promise.race([
                    page.waitForSelector('[data-tid="prejoin-join-button"]', { 
                        hidden: true, 
                        timeout: 120000 // 2 minutes for manual CAPTCHA solving
                    }),
                    page.waitForSelector('#chat-button', { 
                        timeout: 120000,
                        visible: true 
                    })
                ]);
                
                console.log("CAPTCHA appears to be resolved, continuing...");
                await this.waitRandom(1000, 2000);
            }
        } catch (error) {
            console.log("No CAPTCHA detected or CAPTCHA timeout, continuing...");
        }

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
        await this.waitRandom(1000, 2000);
        try {
            await this.humanClick(page, "#chat-button");
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
        await this.waitRandom(800, 1500);
        await this.sendMessages(page, details.introMessages);

        console.log("Opening view panel.");
        await this.waitRandom(1000, 2000);
        await this.humanClick(page, "#custom-view-button");

        console.log("Selecting speaker view.");
        await this.waitRandom(600, 1200);
        await this.humanClick(page, "#SpeakerView-button");

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

        await page.exposeFunction("speakerChange", async (speaker: string) => {
            console.log(`DEBUG: Speaker detected: "${speaker}"`);
            await transcriptionService.speakerChange(speaker);
        });
        
        console.log("Listening for speaker changes.");
        await page.evaluate(() => {
            console.log('DEBUG: Setting up speaker detection for both normal and screen sharing modes...');
            
            const findCurrentSpeaker = () => {
                // Method 1: Look for active speaking indicator (voice-level-stream-outline with vdi-frame-occlusion class)
                const speakingIndicator = document.querySelector('[data-tid="voice-level-stream-outline"].vdi-frame-occlusion');
                if (speakingIndicator) {
                    console.log('DEBUG: Found active speaking indicator, looking for associated participant...');
                    
                    // Find the participant container that contains this speaking indicator
                    const participantContainer = speakingIndicator.closest('[data-tid]:not([data-tid*="wrapper"]):not([data-tid*="button"]):not([data-tid*="avatar"]):not([data-tid*="outline"])');
                    if (participantContainer) {
                        const name = participantContainer.getAttribute('data-tid');
                        if (name && name.length > 0 && !name.includes('LMA')) {
                            console.log(`DEBUG: Active speaking indicator mode - speaker: "${name}"`);
                            return name;
                        }
                    }
                    
                    // Alternative: look for the participant element that's a sibling or parent
                    const participantElement = speakingIndicator.parentElement?.querySelector('[data-tid]:not([data-tid*="wrapper"]):not([data-tid*="button"]):not([data-tid*="avatar"]):not([data-tid*="outline"])');
                    if (participantElement) {
                        const name = participantElement.getAttribute('data-tid');
                        if (name && name.length > 0 && !name.includes('LMA')) {
                            console.log(`DEBUG: Active speaking indicator (sibling) mode - speaker: "${name}"`);
                            return name;
                        }
                    }
                }
                
                // Method 2: Check for screen sharing scenario
                const screenShareElement = document.querySelector('[data-stream-type="ScreenSharing"]');
                if (screenShareElement) {
                    console.log('DEBUG: Screen sharing detected, looking for speaker in video participants...');
                    
                    // When screen sharing, look for video participants (not the screen sharer)
                    const videoParticipants = document.querySelectorAll('[data-stream-type="Video"][data-tid]:not([data-tid*="LMA"])');
                    console.log(`DEBUG: Found ${videoParticipants.length} video participants during screen share`);
                    
                    for (const participant of videoParticipants) {
                        const name = participant.getAttribute('data-tid');
                        if (name && name.length > 0) {
                            console.log(`DEBUG: Screen sharing mode - speaker: "${name}"`);
                            return name;
                        }
                    }
                }
                
                // Method 3: Normal mode - check SpeakerStage-wrapper
                const speakerStage = document.querySelector('[data-tid="SpeakerStage-wrapper"]');
                if (speakerStage) {
                    const speakerElement = speakerStage.querySelector('[data-tid]:not([data-tid*="wrapper"]):not([data-tid*="button"]):not([data-tid*="avatar"])');
                    if (speakerElement) {
                        const name = speakerElement.getAttribute('data-tid');
                        if (name && name.length > 0) {
                            console.log(`DEBUG: Normal mode - speaker: "${name}"`);
                            return name;
                        }
                    }
                }
                
                // Method 4: Fallback - look for any prominent participant
                const allParticipants = document.querySelectorAll('[data-tid]:not([data-tid*="wrapper"]):not([data-tid*="button"]):not([data-tid*="avatar"]):not([data-tid*="LMA"])');
                console.log(`DEBUG: Fallback - found ${allParticipants.length} total participants`);
                
                for (const participant of allParticipants) {
                    const name = participant.getAttribute('data-tid');
                    if (name && name.length > 0 && name.length < 100) {
                        console.log(`DEBUG: Fallback mode - speaker: "${name}"`);
                        return name;
                    }
                }
                
                console.log('DEBUG: No speaker found in any mode');
                return '';
            };
            
            const targetNode = document.querySelector('[data-tid="modern-stage-wrapper"]');
            const config = { 
                childList: true,
                subtree: true,
                characterData: true,
                attributes: true,
                attributeFilter: ['data-tid', 'class', 'data-stream-type']
            };
            
            const callback = (mutationList: MutationRecord[]) => {
                console.log(`DEBUG: Stage mutation detected, checking for speaker...`);
                const currentSpeaker = findCurrentSpeaker();
                if (currentSpeaker) {
                    (window as any).speakerChange(currentSpeaker);
                }
            };
            
            const observer = new MutationObserver(callback);
            if (targetNode) {
                observer.observe(targetNode, config);
                console.log('DEBUG: MutationObserver set up on modern-stage-wrapper');
            }

            // Set initial speaker
            const initialSpeaker = findCurrentSpeaker();
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
