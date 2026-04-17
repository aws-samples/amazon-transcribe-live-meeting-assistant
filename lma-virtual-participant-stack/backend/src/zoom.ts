import { Page,ConsoleMessage } from 'puppeteer';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';
import { voiceAssistant } from './voice-assistant.js';
import { simliAvatar } from './simli-avatar.js';

export default class Zoom {
    private async waitForButtonWithRetry(
        page: Page,
        selectors: string[],
        maxRetries: number = 10,
        delayMs: number = 500
    ): Promise<{ element: any; selector: string } | null> {
        for (let attempt = 1; attempt <= maxRetries; attempt++) {
            console.log(`Attempt ${attempt}/${maxRetries} to find button...`);
            
            for (const selector of selectors) {
                const element = await page.$(selector);
                if (element) {
                    console.log(`Found button with selector: ${selector}`);
                    return { element, selector };
                }
            }
            
            if (attempt < maxRetries) {
                console.log(`Buttons not found, waiting ${delayMs}ms before retry...`);
                await new Promise(resolve => setTimeout(resolve, delayMs));
            }
        }
        
        console.log(`Failed to find any button after ${maxRetries} attempts`);
        return null;
    }

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
                    enterpriseLogin = true;
                    
                    // Notify frontend that manual action is required
                    const { details } = await import('./details.js');
                    if (details.invite.virtualParticipantId) {
                        const { createStatusManager } = await import('./status-manager.js');
                        const statusManager = createStatusManager(details.invite.virtualParticipantId);
                        await statusManager.setManualActionRequired(
                            'LOGIN',
                            'Enterprise Zoom authentication required. Please sign in using the VNC viewer.',
                            120
                        );
                    }
                    
                    await page.waitForSelector('.video-avatar__avatar', { timeout: 120000 }); // Give 2 minutes to login
                    await new Promise(resolve => setTimeout(resolve, 5000));
                    
                    // Clear manual action notification after successful login
                    if (details.invite.virtualParticipantId) {
                        const { createStatusManager } = await import('./status-manager.js');
                        const statusManager = createStatusManager(details.invite.virtualParticipantId);
                        await statusManager.clearManualAction();
                    }
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

            console.log('Checking audio button state with retry...');
            // Wait for audio button to appear (handles loading state)
            const audioResult = await this.waitForButtonWithRetry(
                page,
                ['svg.SvgAudioMute', 'svg.SvgAudioUnmute']
            );
            
            if (audioResult && !voiceAssistant.isEnabled()) {
                if (audioResult.selector === 'svg.SvgAudioMute') {
                    console.log('Audio is unmuted, clicking to mute it.');
                    await audioResult.element.click();
                } else {
                    console.log('Audio is already muted, skipping click.');
                }
            } else if (voiceAssistant.isEnabled()) {
                console.log('Voice assistant enabled - keeping microphone unmuted for agent audio');
                if (audioResult && audioResult.selector === 'svg.SvgAudioUnmute') {
                    console.log('Audio is muted, clicking to unmute it for voice assistant.');
                    await audioResult.element.click();
                }
            } else {
                console.log('Warning: Could not find audio button in either state after retries.');
            }

            console.log('Checking video button state with retry...');
            // Wait for video button to appear (handles loading state)
            const videoResult = await this.waitForButtonWithRetry(
                page,
                ['svg.SvgVideoOn', 'svg.SvgVideoOff']
            );
            
            if (videoResult && simliAvatar.isConnected()) {
                // Simli avatar active - keep video ON so avatar shows as camera
                if (videoResult.selector === 'svg.SvgVideoOff') {
                    console.log('Simli avatar active - clicking to turn video ON for avatar camera.');
                    await videoResult.element.click();
                } else {
                    console.log('Simli avatar active - video is already on, good.');
                }
            } else if (videoResult) {
                if (videoResult.selector === 'svg.SvgVideoOn') {
                    console.log('Video is on, clicking to turn it off.');
                    await videoResult.element.click();
                } else {
                    console.log('Video is already off, skipping click.');
                }
            } else {
                console.log('Warning: Could not find video button in either state after retries.');
            }

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

        // Dismiss any Zoom popups (recording consent, language interpretation, etc.)
        console.log('Setting up Zoom popup auto-dismiss handler.');
        await page.evaluate(() => {
            // Text patterns that indicate a dismissible consent/info popup.
            // Only modals whose text matches one of these will be auto-dismissed.
            const POPUP_TEXT_PATTERNS = [
                'recording',
                'consent',
                'recorded',
                'language interpretation',
                'translation',
                'request language',
                'by joining',
                'acknowledg',
            ];

            // Modal container selectors - only look inside actual overlay modals,
            // never match toolbar buttons or the main meeting UI.
            const MODAL_SELECTORS = [
                '.zm-modal',
                '.zm-modal-legacy',
                '[role="alertdialog"]',
                '.ReactModal__Content',
                '.recording-disclaimer-dialog',
            ];

            const checkAndDismissPopups = (): boolean => {
                let dismissed = false;

                for (const modalSel of MODAL_SELECTORS) {
                    const modals = document.querySelectorAll(modalSel);
                    modals.forEach((modal) => {
                        const modalEl = modal as HTMLElement;
                        // Skip invisible/hidden modals
                        if (!modalEl || modalEl.offsetParent === null) return;

                        const modalText = modalEl.textContent?.toLowerCase() || '';
                        const isRelevantPopup = POPUP_TEXT_PATTERNS.some(
                            pattern => modalText.includes(pattern)
                        );
                        if (!isRelevantPopup) return;

                        // Try to find and click the primary action button within this modal
                        const actionButtonSelectors = [
                            '.zm-modal-footer-default-actions button.zm-btn--primary',
                            'button.zm-btn--primary',
                            'button.zm-btn-legacy.zm-btn--primary',
                            'button.zm-btn__outline--blue',
                        ];

                        for (const btnSel of actionButtonSelectors) {
                            const btn = modalEl.querySelector(btnSel) as HTMLElement;
                            if (btn && btn.offsetParent !== null) {
                                console.log(`[LMA] Auto-dismissing popup: "${modalText.substring(0, 80).trim()}...", clicking: "${btn.textContent?.trim()}"`);
                                btn.click();
                                dismissed = true;
                                return;
                            }
                        }

                        // Fallback: find any button with dismiss-like text
                        const allButtons = modalEl.querySelectorAll('button');
                        for (const btn of allButtons) {
                            const btnText = btn.textContent?.trim().toLowerCase() || '';
                            if (['got it', 'i agree', 'ok', 'okay', 'continue', 'accept', 'consent', 'agree', 'close', 'dismiss'].includes(btnText)) {
                                console.log(`[LMA] Auto-dismissing popup by button text: "${btn.textContent?.trim()}"`);
                                (btn as HTMLElement).click();
                                dismissed = true;
                                return;
                            }
                        }
                    });
                }

                return dismissed;
            };

            // Check immediately in case popup is already present
            checkAndDismissPopups();

            // Set up MutationObserver to catch popups that appear after join
            const observer = new MutationObserver(() => {
                checkAndDismissPopups();
            });

            observer.observe(document.body, {
                childList: true,
                subtree: true,
            });

            // Also poll periodically as a safety net (some popups may not trigger mutations reliably)
            let pollCount = 0;
            const maxPolls = 60; // Poll for up to 30 seconds (500ms interval)
            const pollInterval = setInterval(() => {
                checkAndDismissPopups();
                pollCount++;
                if (pollCount >= maxPolls) {
                    clearInterval(pollInterval);
                    // Keep the MutationObserver running for popups that may appear later
                }
            }, 500);

            // Store cleanup function for later if needed
            (window as any).__lmaPopupDismissCleanup = () => {
                observer.disconnect();
                clearInterval(pollInterval);
            };
        });

        // Give a brief moment for any popup to appear and be dismissed
        await new Promise(resolve => setTimeout(resolve, 2000));
        console.log('Popup handler active, proceeding to open chat.');

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
        await page.evaluate((vpIdentity: string) => {
            let observer: MutationObserver | null = null;
            let lastSpeaker: string | null = null;
            let micActivityTimeout: any = null;
            let isVPSpeaking = false;
            const MIC_ACTIVITY_THRESHOLD = 5; // px
            const MIC_SILENCE_DELAY = 2000; // ms - increased to 2 seconds to handle pauses in speech

            // Function to get current speaker from any view
            function getCurrentSpeaker(): string | null {
                const selectors = [
                    // Normal mode - main view (prioritized: shows active speaker)
                    '.single-main-container__video-frame .video-avatar__avatar-footer span',
                    // Screen sharing mode - suspension window (small video)
                    '.single-suspension-container__video-frame .video-avatar__avatar-footer span',
                    // Fallback - any avatar footer
                    '.video-avatar__avatar-footer span[role="none"]'
                ];
                
                let vpName: string | null = null;
                for (const selector of selectors) {
                    const element = document.querySelector(selector);
                    const name = element?.textContent?.trim();
                    if (name) {
                        // Skip the VP's own name - we want the OTHER participant
                        if (name === vpIdentity) {
                            vpName = name;
                            continue;
                        }
                        return name;
                    }
                }
                // If only the VP name was found (VP is the only/active speaker), return it
                return vpName;
            }

            function notifySpeakerChange(speaker: string) {
                if (speaker && speaker !== lastSpeaker) {
                    console.log('Speaker changed from', lastSpeaker, 'to', speaker);
                    lastSpeaker = speaker;
                    (window as any).speakerChange(speaker);
                }
            }

            function setupObserver() {
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

                const config = { 
                    childList: true, 
                    subtree: true,
                    characterData: true,
                    attributes: true,
                    attributeFilter: ['style']
                };

                const callback = (mutationList: MutationRecord[]) => {
                    // Check for microphone activity (VP speaking)
                    const micIndicator = document.querySelector('.audio-level-indicator') as HTMLElement;
                    if (micIndicator) {
                        const height = parseFloat(micIndicator.style.height || '0');
                        
                        if (height > MIC_ACTIVITY_THRESHOLD) {
                            // VP is speaking
                            if (!isVPSpeaking) {
                                isVPSpeaking = true;
                                notifySpeakerChange(vpIdentity);
                            }
                            
                            // Reset silence timer - but don't trigger speaker change yet
                            if (micActivityTimeout) {
                                clearTimeout(micActivityTimeout);
                            }
                            micActivityTimeout = setTimeout(() => {
                                // VP stopped speaking - revert to screen speaker
                                if (isVPSpeaking) {
                                    isVPSpeaking = false;
                                    const screenSpeaker = getCurrentSpeaker();
                                    if (screenSpeaker) {
                                        notifySpeakerChange(screenSpeaker);
                                    }
                                }
                            }, MIC_SILENCE_DELAY);
                        }
                    }
                    
                    // Also check for speaker changes from other participants
                    // Only update if VP is not currently speaking
                    if (!isVPSpeaking) {
                        const speaker = getCurrentSpeaker();
                        if (speaker) {
                            notifySpeakerChange(speaker);
                        }
                    }
                };

                observer = new MutationObserver(callback);
                observer.observe(parentNode, config);

                // Handle initial state
                const initialSpeaker = getCurrentSpeaker();
                if (initialSpeaker) {
                    console.log('Initial speaker:', initialSpeaker);
                    lastSpeaker = initialSpeaker;
                    (window as any).speakerChange(initialSpeaker);
                }
            }

            // Wait for Zoom UI to be ready, then setup observer
            setTimeout(setupObserver, 2000);
        }, details.scribeIdentity);

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
                // Detect meeting-end screen (post-meeting "return to home" button).
                // Note: The popup auto-dismiss handler above ensures recording consent
                // and other popup buttons are clicked and removed before we reach here.
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
