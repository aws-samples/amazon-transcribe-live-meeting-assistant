/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page } from 'playwright-core';

import { details, matchesEndCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from "./details.js";
import { transcriptionService } from "./scribe.js";
import { createStatusManager } from "./status-manager.js";
import { voiceAssistant } from './voice-assistant.js';
import { simliAvatar } from './simli-avatar.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { findElementWithFallback, classifyJoinState, isResolverEnabled } from './ai-dom-resolver.js';
import { startDialogWatchdog } from './dialog-watchdog.js';
import { humanClick, humanType } from './prejoin-actions.js';

export default class Teams {
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};

    constructor() {
        this.endRequested = new Promise<ExitInfo>((resolve) => {
            this.requestEnd = resolve;
        });
    }

    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const found = await findElementWithFallback(
            page,
            ['.ck-placeholder'],
            {
                intent: 'Microsoft Teams in-meeting chat compose input area',
                platform: 'TEAMS',
                step: 'teams.chat.input',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!found) {
            console.log('Could not locate Teams chat input — aborting sendMessages');
            return;
        }
        for (const message of messages) {
            await found.element.click();
            await new Promise((resolve) => setTimeout(resolve, 500));
            await found.element.type(message);
            await found.element.press("Enter");
        }
    }

    /**
     * Robust "are we inside the meeting?" check for Teams. Anonymous joins run
     * in the lightweight "light-meetings" client whose in-meeting DOM differs
     * from the full v2 client, and Teams renames its ids/classes across
     * versions — so relying on a single selector (the old `#chat-button`)
     * strands the VP even though it was admitted (avatar visible, but the
     * admission poll never matched). Check a BROAD set of in-meeting signals
     * spanning both clients, and treat the pre-join "Join now" button still
     * being visible as a hard "not in yet". Any positive in-meeting signal
     * counts. Wrapped in .catch so a navigation-destroyed context (the
     * light-meetings → full-client hop right after admission) reads as "not yet"
     * instead of throwing out of the poll loop.
     */
    private async isInMeeting(page: Page): Promise<boolean> {
        return page
            .evaluate(() => {
                // Still on the pre-join screen? Then definitely not in-meeting.
                const onPrejoin = document.querySelector('[data-tid="prejoin-join-button"]');
                if (onPrejoin) {
                    const r = (onPrejoin as HTMLElement).getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return false;
                }

                const visible = (sel: string): boolean => {
                    const el = document.querySelector(sel) as HTMLElement | null;
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const inMeetingSignals = [
                    '#chat-button',                              // v2 client chat toggle
                    '#hangup-button',                           // leave/hangup control
                    '#roster-button',                           // participants/people panel
                    'button[data-inp="roster-button"]',
                    '#custom-view-button',                      // view switcher
                    '#microphone-button',                       // in-meeting mic toggle
                    '#video-button',                            // in-meeting camera toggle
                    '[data-tid="toggle-mute"][data-tid]',       // light-meetings in-call mute
                    '[data-tid="calling-toolbar"]',             // light-meetings call toolbar
                    '[data-tid="call-roster"]',
                    '[data-tid="chat-button"]',
                    '[data-tid="hangup-button"]',
                    '[data-tid="modern-stage-wrapper"]',        // speaker/gallery stage
                    '[data-tid="callingScreen"]',
                    'button[aria-label*="leave" i]',
                    'button[aria-label*="hang up" i]',
                    'button[aria-label*="chat" i]',
                    'button[aria-label*="people" i]',
                ];
                return inMeetingSignals.some(visible);
            })
            .catch(() => false);
    }

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        // Teams has no heavy credentialled sign-in phase, so bring the Simli
        // avatar up now (timing unchanged from before the deferral refactor).
        if (opts.prepareAvatar) await opts.prepareAvatar();
        // AI-driven dialog watchdog runs for the entire meeting lifecycle —
        // sign-in pages, pre-join, waiting-room, and in-meeting. Catches
        // recording-consent / language-interpretation / bot-detection /
        // CAPTCHA / SSO / "stay signed in?" / etc. dialogs that the
        // hardcoded selector list can't classify. CONSENT-class dialogs
        // are auto-dismissed; CAPTCHA/BLOCKED/LOGIN_REQUIRED escalates to
        // MANUAL_ACTION_REQUIRED so the user can clear it via VNC.
        startDialogWatchdog(page, { platform: 'TEAMS' });

        // Push a human-readable sub-step into the JOINING status so the long
        // join span (navigate → redirect chain → prejoin → admission → chat)
        // doesn't look frozen in the UI — mirrors the Zoom handler. Best-effort,
        // no-op without a vpId, never throws or blocks the join.
        const substep = async (message: string): Promise<void> => {
            if (!details.invite.virtualParticipantId) return;
            try {
                await createStatusManager(details.invite.virtualParticipantId).setJoiningSubstep(message);
            } catch {
                /* best-effort progress */
            }
        };

        await substep('Entering the meeting room…');
        try {
            console.log("Getting meeting link.");
            await page.goto(
                `https://teams.microsoft.com/v2/?meetingjoin=true#/meet/${details.invite.meetingId}?p=${details.invite.meetingPassword}&anon=true`
            );
        } catch {
            console.log("Your scribe was unable to join the meeting.");
            return { reason: 'never-joined', trigger: 'pre-join:goto-failed' };
        }

        // For an anonymous join, Teams does NOT land on the pre-join screen
        // immediately. It first fires a silent SSO probe (which fails with
        // AADSTS50058 for an anon session), then bounces the main frame through
        // /dl/launcher/launcher.html and /light-meetings/launch?lightExperience=true
        // before the lightweight web pre-join UI finally renders. This redirect
        // storm takes several seconds, during which the DOM is empty. Wait for
        // network to go idle so we don't start polling (and fire the AI fallback
        // against a blank page) mid-redirect. Best-effort — the long retry
        // budget below is the real backstop.
        try {
            await page.waitForLoadState('networkidle', { timeout: 30000 });
        } catch {
            // networkidle can legitimately never settle on the Teams SPA; the
            // findElementWithFallback retry loop handles the wait either way.
        }

        await substep('Loading the meeting join screen…');
        console.log("Entering name.");
        // Candidate selectors span both the v2 client and the lightweight anon
        // ("light-meetings") client that anonymous joins are routed into.
        // useScreenshot lets the AI fallback recover even when the DOM snapshot
        // is momentarily empty mid-redirect. The retry budget (60 × 1s = 60s)
        // must outlast the silent-SSO → launcher → light-meetings redirect chain
        // and the heavy SPA load; it stays well under waitingTimeout (5 min).
        const nameRes = await findElementWithFallback(
            page,
            [
                '[data-tid="prejoin-display-name-input"]',
                '#prejoin-display-name',
                'input[data-tid="prejoin-display-name-input"]',
            ],
            {
                intent: 'Teams pre-join screen display-name input field',
                platform: 'TEAMS',
                step: 'teams.join.name',
                useScreenshot: true,
            },
            { maxRetries: 60, delayMs: 1000 },
        );
        if (!nameRes) {
            console.log('Could not locate Teams display-name input — aborting join');
            return { reason: 'never-joined', trigger: 'pre-join:no-name-input' };
        }
        // humanType focuses the field in the DOM and types via the keyboard,
        // bypassing Playwright's actionability/pointer-events hit-test. The
        // Teams light-meetings pre-join floats a transient shroud over the form
        // while it finishes hydrating, and a plain ElementHandle.type() throws
        // "failed pointer_events check: element is covered by <unknown>". Clear
        // first, then verify the value landed and re-type once (the shroud can
        // swallow the first keystroke burst), mirroring the Zoom handler.
        await nameRes.element.evaluate((el: Element) => {
            const i = el as HTMLInputElement;
            i.focus();
            i.value = '';
        });
        await humanType(page, nameRes.element, details.scribeIdentity);
        const typedName = await nameRes.element.evaluate(
            (el: Element) => (el as HTMLInputElement).value || '',
        );
        if (typedName !== details.scribeIdentity) {
            console.warn(
                `Display-name value mismatch (expected "${details.scribeIdentity}", got "${typedName}") — clearing and re-typing.`,
            );
            await nameRes.element.evaluate((el: Element) => {
                const i = el as HTMLInputElement;
                i.focus();
                i.value = '';
            });
            await humanType(page, nameRes.element, details.scribeIdentity);
        }
        // The field already holds focus from humanType, so press Enter via the
        // keyboard (no pointer hit-test) to commit any name autocomplete.
        await page.keyboard.press('Enter');

        // Only click mute button if voice assistant is NOT enabled
        if (!voiceAssistant.isEnabled()) {
            await new Promise((resolve) => setTimeout(resolve, 250));
            console.log("Clicking mute button.");
            const muteRes = await findElementWithFallback(
                page,
                ['[data-tid="toggle-mute"]'],
                {
                    intent: 'Teams pre-join screen microphone mute toggle',
                    platform: 'TEAMS',
                    step: 'teams.join.muteToggle',
                },
                { maxRetries: 10, delayMs: 500 },
            );
            if (muteRes) await humanClick(page, muteRes.element);
        } else {
            console.log('Voice assistant enabled - skipping mute button for agent audio');
        }

        await new Promise((resolve) => setTimeout(resolve, 250));
        if (simliAvatar.isConnected()) {
            console.log("Simli avatar active - keeping video ON for avatar camera.");
            // Don't click the video toggle - leave it on so Simli avatar shows
        } else {
            console.log("Clicking video button to turn off.");
            const videoRes = await findElementWithFallback(
                page,
                ['[data-tid="toggle-video"]'],
                {
                    intent: 'Teams pre-join screen camera/video toggle',
                    platform: 'TEAMS',
                    step: 'teams.join.videoToggle',
                },
                { maxRetries: 10, delayMs: 500 },
            );
            if (videoRes) await humanClick(page, videoRes.element);
        }

        await new Promise((resolve) => setTimeout(resolve, 250));
        await substep('Setting up audio and video…');
        console.log("Clicking join button.");
        const joinRes = await findElementWithFallback(
            page,
            ['[data-tid="prejoin-join-button"]'],
            {
                intent: 'Teams pre-join screen primary "Join now" button',
                platform: 'TEAMS',
                step: 'teams.join.joinButton',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!joinRes) {
            console.log('Could not locate Teams Join button — aborting');
            return { reason: 'never-joined', trigger: 'pre-join:no-join-button' };
        }
        // humanClick bypasses the pointer_events hit-test that fails when Teams
        // floats a transient overlay over the "Join now" button.
        await humanClick(page, joinRes.element);

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
                        state: 'hidden',
                        timeout: 120000 // 2 minutes for manual CAPTCHA solving
                    }),
                    page.waitForSelector('#chat-button', {
                        timeout: 120000,
                        state: 'visible'
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

        await substep('Waiting to be admitted to the meeting…');
        // Poll for any in-meeting signal rather than a single waitForSelector on
        // a brittle id. The old `#chat-button` wait both (a) missed the
        // light-meetings anon client (different DOM than the full v2 client) and
        // (b) THREW early — not timed out — when the post-CAPTCHA navigation from
        // light-meetings into the full client destroyed its execution context,
        // so an actually-admitted VP (avatar visible) reported "not admitted"
        // after ~50s. Mirror the Zoom admission poll: fast CSS heuristic every
        // 1.5s, backed by Claude's vision join-state classifier every 30s (so a
        // Teams DOM rename can't strand us), with MANUAL_ACTION deadline grace.
        const POLL_INTERVAL_MS = 1500;
        const startWait = Date.now();
        const baseDeadline = startWait + details.waitingTimeout;
        const MANUAL_ACTION_GRACE_MS = 5 * 60 * 1000;
        const AI_CHECK_INTERVAL_MS = 30_000;
        let lastAiCheck = Date.now();
        let lastProgressBump = Date.now();
        let admitted = false;
        const sm = details.invite.virtualParticipantId
            ? createStatusManager(details.invite.virtualParticipantId)
            : null;
        while (true) {
            if (page.isClosed()) break;
            try {
                if (await this.isInMeeting(page)) {
                    admitted = true;
                    break;
                }
            } catch {
                /* ignore — page may be navigating (light-meetings → full client) */
            }
            if (isResolverEnabled() && Date.now() - lastAiCheck > AI_CHECK_INTERVAL_MS) {
                lastAiCheck = Date.now();
                try {
                    const verdict = await classifyJoinState(page, { platform: 'TEAMS' });
                    if (verdict) {
                        console.log(`[teams] AI join-state check: ${verdict.state} — ${verdict.reason}`);
                        if (verdict.state === 'in-meeting') {
                            console.log('[teams] AI confirms we are in the meeting (CSS heuristic missed it) — proceeding.');
                            admitted = true;
                            break;
                        }
                        if (verdict.state === 'error') {
                            console.warn(`[teams] AI detected an error/blocked screen during admission wait: ${verdict.reason}`);
                            return { reason: 'never-joined', trigger: 'pre-join:ai-error-screen' };
                        }
                    }
                } catch (e) {
                    console.warn('[teams] AI join-state check failed (non-fatal):', e);
                }
            }
            // Keep the JOINING detail fresh so the long admission wait never
            // looks frozen. Skip while MANUAL_ACTION is active (CAPTCHA banner).
            if (Date.now() - lastProgressBump > 20000) {
                lastProgressBump = Date.now();
                const secs = Math.round((Date.now() - startWait) / 1000);
                const current = sm ? await sm.getCurrentStatus().catch(() => null) : null;
                if (current !== 'MANUAL_ACTION_REQUIRED') {
                    await substep(`Waiting to be admitted to the meeting… (${secs}s — host may need to admit the participant)`);
                }
            }
            // Extend the deadline while a manual action (CAPTCHA) is being solved
            // in VNC, so the wait doesn't expire mid-solve.
            let extended = false;
            if (sm && Date.now() > baseDeadline) {
                try {
                    const current = await sm.getCurrentStatus();
                    if (current === 'MANUAL_ACTION_REQUIRED' && Date.now() < baseDeadline + MANUAL_ACTION_GRACE_MS) {
                        extended = true;
                    }
                } catch {
                    /* couldn't read status; fall through */
                }
            }
            if (Date.now() > baseDeadline && !extended) {
                console.log("Your scribe was not admitted into the meeting.");
                return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
            }
            await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        }
        if (!admitted) {
            console.log("Your scribe was not admitted into the meeting.");
            return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
        }

        // Admitted. Open the chat panel (best-effort — the intro/chat flow is
        // resilient to the panel toggle not being found via the primary id).
        console.log("Opening chat panel.");
        try {
            const chatRes = await findElementWithFallback(
                page,
                ['#chat-button', '[data-tid="chat-button"]'],
                {
                    intent: 'Teams in-meeting toolbar button that opens the chat panel',
                    platform: 'TEAMS',
                    step: 'teams.meeting.chatButton',
                },
                { maxRetries: 8, delayMs: 500 },
            );
            if (chatRes) await humanClick(page, chatRes.element);
            else console.log('Chat button not found post-admission — continuing (intro may not post).');
        } catch (e) {
            console.warn('Opening chat panel failed (non-fatal):', e);
        }

        // Update status to JOINED
        if (details.invite.virtualParticipantId) {
            const statusManager = createStatusManager(details.invite.virtualParticipantId);
            await statusManager.setJoined();
        }
        await substep('In the meeting — posting introduction…');
        console.log("Sending introduction messages.");
        await this.sendMessages(page, details.introMessages);

        // Switch to speaker view — best-effort and SHORT-timed. This is a
        // cosmetic preference (better speaker attribution / video framing), not
        // required to be in the meeting and transcribe. The full-client ids
        // (#custom-view-button / #SpeakerView-button) don't exist in the
        // light-meetings anon client, so a hard waitForSelector here would block
        // the intro + transcription for up to waitingTimeout (5 min) or throw
        // and abort an otherwise-successful join. Try briefly, then move on.
        console.log("Opening view panel (best-effort).");
        try {
            const viewPanelElement = await page.waitForSelector("#custom-view-button", {
                timeout: 5000,
            });
            await viewPanelElement?.click();
            console.log("Selecting speaker view.");
            const speakerViewElement = await page.waitForSelector("#SpeakerView-button", {
                timeout: 5000,
            });
            await speakerViewElement?.click();
        } catch {
            console.log("View/speaker-view controls not found (likely light-meetings client) — skipping.");
        }

        // Set up simple attendee change monitoring
        await page.exposeFunction('attendeeChange', async (hasOthers: boolean) => {
            console.log(`DEBUG: Teams has other participants: ${hasOthers}`);
            if (!hasOthers) {
                console.log('LMA Virtual Participant got lonely and left.');
                details.start = false;
                this.requestEnd({ reason: 'alone-in-meeting', trigger: 'attendees-left' });
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

        // Speaker attribution combines two signals:
        //   1) Page-side DOM MutationObserver — identifies human speakers via
        //      Teams' voice-level-stream-outline + vdi-frame-occlusion classes.
        //   2) AgentSpeakingDetector — emits started/stopped events based on
        //      RMS of the voice agent's PCM output on agent_output.monitor.
        //
        // When the detector says the agent is speaking we attribute to LMA;
        // otherwise we report the last human DOM speaker.

        let lastMeetingSpeaker: string | null = null;
        let lastReportedSpeaker: string | null = null;

        const reportSpeaker = async (speaker: string) => {
            if (!speaker || speaker === lastReportedSpeaker) return;
            lastReportedSpeaker = speaker;
            await transcriptionService.speakerChange(speaker);
        };

        await page.exposeFunction("speakerChange", async (speaker: string) => {
            if (!speaker) return;
            lastMeetingSpeaker = speaker;
            await reportSpeaker(speaker);
        });

        if (voiceAssistant.isEnabled()) {
            const onAgentStart = () => {
                reportSpeaker(details.scribeIdentity).catch(() => {});
            };
            const onAgentStop = () => {
                if (lastMeetingSpeaker) {
                    reportSpeaker(lastMeetingSpeaker).catch(() => {});
                }
            };
            agentSpeakingDetector.on('started', onAgentStart);
            agentSpeakingDetector.on('stopped', onAgentStop);
            if (agentSpeakingDetector.isSpeaking()) onAgentStart();

            page.once('close', () => {
                agentSpeakingDetector.off('started', onAgentStart);
                agentSpeakingDetector.off('stopped', onAgentStop);
            });
        }

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
                
                // No active speaking indicator and no screen share — return
                // empty so the caller leaves the current speaker unchanged.
                // Avoid returning any "first participant tile" fallback here,
                // which would emit meaningless data-tid values (e.g. the
                // meeting-branding wrapper) while the agent is talking.
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
            // Teams chat-message text doesn't expose sender on the same node;
            // best-effort sender extraction would require additional DOM
            // wiring. For now we use the lenient matcher and a generic
            // farewell.
            if (matchesEndCommand(message)) {
                console.log(`LMA Virtual Participant has been asked to leave the meeting: ${JSON.stringify(message)}`);
                try {
                    await this.sendMessages(page, exitMessagesFor(null));
                } catch (e) {
                    // Best effort — fall through to ending the meeting.
                    console.warn('Could not send goodbye message:', e);
                }
                details.start = false;
                // Hand off to the wait-for-meeting-end race below; the
                // orchestrator's cleanup chain in index.ts owns the browser
                // close so we don't orphan the exposed-function callback.
                this.requestEnd({
                    reason: 'end-command',
                    trigger: 'chat',
                    matchedMessage: message,
                });
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
        let exitInfo: ExitInfo = { reason: 'unknown' };
        try {
            // Race the in-process end signal (chat command, etc.) against
            // multiple Teams meeting-end UI indicators. Each UI branch maps
            // to a structured ExitInfo so the orchestrator can persist and
            // log a single canonical reason.
            const hangupHidden: Promise<ExitInfo> = page
                .waitForSelector("#hangup-button", { state: 'hidden', timeout: details.meetingTimeout })
                .then((): ExitInfo => ({ reason: 'host-ended', trigger: 'HANGUP_BUTTON_HIDDEN' }));
            const rejoinAppeared: Promise<ExitInfo> = page
                .waitForSelector('button[data-tid="anon-meeting-end-screen-rejoin-button"]', { timeout: details.meetingTimeout })
                .then((): ExitInfo => ({ reason: 'host-ended', trigger: 'REJOIN_BUTTON_APPEARED' }));
            const urlBlank: Promise<ExitInfo> = page
                .waitForFunction(() => window.location.href === 'about:blank', undefined, { timeout: details.meetingTimeout })
                .then((): ExitInfo => ({ reason: 'page-closed', trigger: 'URL_CHANGE_BLANK' }));

            exitInfo = await Promise.race([this.endRequested, hangupHidden, rejoinAppeared, urlBlank]);
        } catch (error) {
            console.log(`DEBUG: Teams meeting timeout error: ${error instanceof Error ? error.message : String(error)}`);
            console.log("Meeting timed out.");
            exitInfo = { reason: 'meeting-timeout', trigger: 'meetingTimeout' };
        } finally {
            details.start = false;
        }
        console.log(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
        return exitInfo;
    }
}
