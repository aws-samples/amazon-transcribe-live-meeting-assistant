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
                `https://teams.microsoft.com/v2/?meetingjoin=true#/meet/${details.invite.meetingId}?p=${details.invite.meetingPassword}&anon=true`
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

        // Wait for potential CAPTCHA with longer timeout
        console.log("Checking for CAPTCHA...");
        await new Promise((resolve) => setTimeout(resolve, 250));
        
        try {
            const captchaElement = await page.waitForSelector(
                '[data-tid="HIP-Captcha-Image"]',
                { timeout: 5000 }
            );
            
            if (captchaElement) {
                console.log("CAPTCHA detected! Waiting for manual resolution...");
                console.log("Please solve the CAPTCHA in the VNC viewer.");
                
                // Notify frontend that manual action is required
                if (details.invite.virtualParticipantId) {
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.setManualActionRequired(
                        'CAPTCHA',
                        'CAPTCHA detected. Please solve the CAPTCHA in the VNC viewer.',
                        120
                    );
                }
                
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
                await new Promise((resolve) => setTimeout(resolve, 250));
                
                // Clear manual action notification after CAPTCHA is resolved
                if (details.invite.virtualParticipantId) {
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.clearManualAction();
                }
            }
        } catch (error) {
            console.log("No CAPTCHA detected or CAPTCHA timeout, continuing...");
        }

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
