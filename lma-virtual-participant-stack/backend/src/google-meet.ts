/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * The Google Meet handler: joins a Meet meeting as a guest from the browser,
 * under the VP's display name, and runs the meeting the way the other browser
 * joins do (Webex and Teams are the closest models).
 *
 *   1. Open https://meet.google.com/<code>?hl=en. A refusal page ("You can't
 *      join this video call") or an invalid code ends the join at once.
 *   2. Fill "What's your name?", turn the microphone (unless a voice assistant
 *      needs it) and the camera (unless the Simli avatar is the camera) off, and
 *      press "Ask to join", or "Join now" where the meeting lets guests straight in.
 *   3. Wait, bounded by `details.waitingTimeout`, until the in-meeting "Leave
 *      call" button appears. A declined or unanswered request, a refusal, and
 *      the deadline each end the join with their own `pre-join:` trigger.
 *   4. In the meeting: post the introduction in the chat ("In-call messages"),
 *      read the chat for LMA's commands, report the active speaker from the
 *      participant tiles, and watch the People count and Meet's end screens.
 *
 * The selectors and the text this relies on are in google-meet-page.ts, with
 * every assumption about Meet's page that still needs a live check marked there.
 */
import { ElementHandle, Page } from 'playwright-core';

import { details, matchesEndCommand, matchesChatCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from './details.js';
import { transcriptionService } from './scribe.js';
import { createStatusManager } from './status-manager.js';
import { voiceAssistant } from './voice-assistant.js';
import { applyAssistantChatCommand, createDuplicateToggleFilter, parseAssistantChatCommand } from './assistant-toggles.js';
import { allChatHelpMessages, chatHelpMessages } from './chat-help.js';
import { attachMeetingConsent, meetingConsent } from './consent-runtime.js';
import { toolbarPanels } from './consent-evidence.js';
import { parseChatQuestion, startChatAnswer } from './chat-ask.js';
import { simliAvatar } from './simli-avatar.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { classifyJoinState, findElementWithFallback, isResolverEnabled } from './ai-dom-resolver.js';
import { startDialogWatchdog } from './dialog-watchdog.js';
import { humanClick, humanType } from './prejoin-actions.js';
import { gotoMeetingPage, MEETING_HOST_PATTERNS } from './meeting-navigation.js';
import {
    AttendeeReading,
    AttendeeWatchdogState,
    DEFAULT_ATTENDEE_WATCHDOG_CONFIG,
    decideAttendeeAction,
} from './teams.js';
import {
    MEET_HOST,
    MEET_IN_PAGE_SELECTORS,
    MEET_SELECTORS,
    MeetScreen,
    MeetTileReading,
    classifyMeetScreenText,
    isMeetRefusalRedirect,
    isOwnMeetName,
    parseGoogleMeetMeeting,
    parseMeetParticipantCount,
    pickMeetActiveSpeaker,
    senderFromMeetGroupHeader,
} from './google-meet-page.js';
import { createLogger } from './logger.js';

const log = createLogger('google-meet');

/** One reading of the page: whether the VP is in the meeting, and what the page says. */
interface MeetPageReading {
    url: string;
    inMeeting: boolean;
    /** A visible "Ask to join" / "Join now" button: still on the pre-join screen. */
    joinButtonVisible: boolean;
    /** The page's visible text, only read while not in the meeting. */
    text: string;
}

/** One chat message as the reader takes it from the page. */
interface MeetChatReading {
    sender: string | null;
    text: string;
}

/**
 * The pre-join exit for a Meet screen, or null when the screen does not end the join.
 *
 * Exported for google-meet.test.ts. Every branch is `never-joined`, which is
 * what keeps a refused or unanswered join from being scored as a meeting the
 * VP attended (exit-reason.test.ts holds every `pre-join:` trigger to that).
 */
export function meetPreJoinExit(screen: MeetScreen | null): ExitInfo | null {
    switch (screen) {
        case 'refused':
            return { reason: 'never-joined', trigger: 'pre-join:meet-refused' };
        case 'denied':
        case 'removed':
            return { reason: 'never-joined', trigger: 'pre-join:meet-denied' };
        case 'no-response':
            return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
        case 'invalid-code':
            return { reason: 'never-joined', trigger: 'pre-join:meet-invalid-code' };
        case 'ended':
        case 'left':
            return { reason: 'never-joined', trigger: 'pre-join:meet-ended' };
        default:
            return null;
    }
}

/**
 * The exit for a Meet screen seen after admission, or null when it does not end the meeting.
 *
 * Only consulted once the "Leave call" button has gone, so chat text that
 * happens to contain one of these phrases is not read as the end of the meeting.
 */
export function meetInMeetingExit(screen: MeetScreen | null): ExitInfo | null {
    switch (screen) {
        case 'removed':
        case 'denied':
            return { reason: 'removed-from-meeting', trigger: 'meet-removed-screen' };
        case 'ended':
            return { reason: 'host-ended', trigger: 'meet-ended-screen' };
        case 'left':
        case 'refused':
            // Meet shows its after-the-call page without the VP having left:
            // Meet itself dropped the VP. The same classification Webex gives
            // its "you have left the meeting" text.
            return { reason: 'page-closed', trigger: 'meet-left-screen' };
        default:
            return null;
    }
}

/**
 * Is this chat message one the VP itself sent?
 *
 * A backstop for messages read without a sender, which Meet's chat can give
 * when the reader cannot find the header of a message's group. Compared with
 * the configured message lists, so a deployment that customises them is covered.
 */
export function isOwnMeetMessage(message: string): boolean {
    const text = message.trim();
    if (!text) return false;
    return (
        [
            ...details.introMessages,
            ...details.startMessages,
            ...details.pauseMessages,
            ...details.exitMessages,
            ...allChatHelpMessages(),
        ].some((own) => own && own.trim() === text) || meetingConsent.isOwnMessage(text)
    );
}

export default class GoogleMeet {
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};
    // Set once the end command has been handled, so a second "LMA leave" read
    // while the goodbye is being posted does not post it twice.
    private endHandled = false;
    // Drops a command read twice in a few seconds (the reader keys on Meet's
    // message id, so this is a backstop for a re-rendered message without one).
    private readonly freshToggle = createDuplicateToggleFilter();
    // Logged once, so a host who turned chat off for participants is visible in
    // the log without a line per message.
    private chatUnavailableLogged = false;

    constructor() {
        this.endRequested = new Promise<ExitInfo>((resolve) => {
            this.requestEnd = resolve;
        });
    }

    /** The VP's own names, for recognising its own tile and chat messages. */
    private ownIdentities(): string[] {
        return [details.scribeIdentity, details.scribeName];
    }

    /**
     * Where the page is. Never throws: a page that is navigating reads as "not
     * in the meeting" with no text, which every caller treats as "look again".
     */
    private async readPage(page: Page): Promise<MeetPageReading> {
        const url = page.url();
        try {
            const reading = await page.evaluate((sels: { leaveCall: readonly string[] }) => {
                const visible = (el: Element | null): boolean => {
                    if (!el) return false;
                    const r = (el as HTMLElement).getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const inMeeting = sels.leaveCall.some((sel) => visible(document.querySelector(sel)));
                const joinButtonVisible = Array.from(document.querySelectorAll('button, [role="button"]')).some(
                    (el) => visible(el) && /^(ask to join|join now)$/i.test((el.textContent || '').replace(/\s+/g, ' ').trim()),
                );
                const text = inMeeting ? '' : (document.body?.innerText || '').slice(0, 6000);
                return { inMeeting, joinButtonVisible, text };
            }, { leaveCall: MEET_IN_PAGE_SELECTORS.leaveCall });
            return { url, ...reading };
        } catch {
            return { url, inMeeting: false, joinButtonVisible: false, text: '' };
        }
    }

    /**
     * Click the first visible match among `selectors`, if any. For optional
     * pre-join controls: no AI fallback, since an absent control is the normal
     * case and guessing at one would click something else.
     */
    private async clickIfPresent(page: Page, selectors: readonly string[], label: string): Promise<boolean> {
        for (const selector of selectors) {
            try {
                const handle = await page.$(selector);
                if (handle && (await handle.isVisible())) {
                    await humanClick(page, handle);
                    log.info(`Clicked ${label} (${selector}).`);
                    return true;
                }
            } catch {
                /* try the next selector */
            }
        }
        return false;
    }

    /**
     * Google's cookie-consent interstitial (consent.google.com), shown to
     * browsers in some regions before any Google page. "Reject all" keeps the
     * VP's profile free of optional cookies. Best effort; absent elsewhere.
     */
    private async passConsentInterstitial(page: Page): Promise<void> {
        if (!/^https:\/\/consent\.google\.com\//i.test(page.url())) return;
        log.info('Google cookie-consent page shown — choosing "Reject all".');
        const clicked = await this.clickIfPresent(
            page,
            ['button:has-text("Reject all")', 'button[aria-label^="Reject all" i]', 'button:has-text("Accept all")'],
            'Google cookie consent',
        );
        if (clicked) await page.waitForURL(/meet\.google\.com/, { timeout: 15_000 }).catch(() => undefined);
    }

    /**
     * Turn the microphone and camera off on the pre-join screen, and answer
     * Meet's "see and hear you" device prompt if it shows one.
     *
     * Meet labels each toggle with the action it would take, so the VP clicks
     * "Turn off …" only when that is what is offered and never toggles a device
     * that is already off. ASSUMPTION (verify live): the labels start "Turn off
     * microphone" / "Turn off camera", followed by the keyboard shortcut.
     */
    private async setPrejoinDevices(page: Page): Promise<void> {
        const keepMic = voiceAssistant.isEnabled();
        const keepCamera = simliAvatar.isConnected();
        // The device prompt. ASSUMPTION (verify live): button wording.
        if (keepMic || keepCamera) {
            await this.clickIfPresent(
                page,
                ['button:has-text("Use microphone and camera")', 'button:has-text("Allow microphone and camera")', 'button:has-text("Use microphone")'],
                'device prompt (use devices)',
            );
        } else {
            await this.clickIfPresent(
                page,
                ['button:has-text("Continue without microphone and camera")', 'button:has-text("Continue without microphone")'],
                'device prompt (continue without devices)',
            );
        }
        if (keepMic) {
            log.info('Voice assistant enabled - leaving the microphone on for agent audio');
        } else {
            await this.clickIfPresent(
                page,
                ['button[aria-label^="Turn off microphone" i]', '[role="button"][aria-label^="Turn off microphone" i]'],
                'microphone off',
            );
        }
        if (keepCamera) {
            log.info('Simli avatar active - keeping the camera on for the avatar.');
        } else {
            await this.clickIfPresent(
                page,
                ['button[aria-label^="Turn off camera" i]', '[role="button"][aria-label^="Turn off camera" i]'],
                'camera off',
            );
        }
    }

    /** Dismiss Meet's one-off tips and notices ("Got it") after joining. Best effort. */
    private async dismissTips(page: Page): Promise<void> {
        for (let i = 0; i < 3; i++) {
            const clicked = await this.clickIfPresent(
                page,
                ['[role="dialog"] button:has-text("Got it")', '[role="dialog"] button:has-text("Dismiss")', 'button:has-text("Got it")'],
                'tip dismissal',
            );
            if (!clicked) return;
            await new Promise((r) => setTimeout(r, 500));
        }
    }

    // ------------------------------------------------------------------ chat

    private async chatInputVisible(page: Page): Promise<boolean> {
        return page
            .evaluate((sels: readonly string[]) => {
                for (const s of sels) {
                    const el = document.querySelector(s) as HTMLElement | null;
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
            }, MEET_IN_PAGE_SELECTORS.chatInput)
            .catch(() => false);
    }

    /**
     * Open the chat panel unless it is open already (the toolbar button is a
     * toggle, so clicking it with the panel open would close it). Never throws.
     */
    private async openChatPanel(page: Page): Promise<boolean> {
        if (await this.chatInputVisible(page)) return true;
        try {
            const button = await findElementWithFallback(
                page,
                [...MEET_SELECTORS.chatButton],
                {
                    intent: 'Google Meet in-meeting toolbar button that opens the chat panel ("Chat with everyone" / in-call messages). NOT the People button, NOT a close button.',
                    platform: 'GOOGLE_MEET',
                    step: 'meet.meeting.chatButton',
                },
                { maxRetries: 6, delayMs: 500 },
            );
            if (!button) {
                log.warn('Could not locate the Meet chat button.');
                return false;
            }
            await humanClick(page, button.element);
        } catch (e) {
            log.warn('Opening the Meet chat panel failed (non-fatal):', e);
            return false;
        }
        const until = Date.now() + 4000;
        while (Date.now() < until) {
            if (await this.chatInputVisible(page)) return true;
            await new Promise((r) => setTimeout(r, 300));
        }
        return this.chatInputVisible(page);
    }

    /**
     * Post chat messages. Non-fatal: the VP is in the meeting by the time this
     * runs, and a host can turn chat off for participants ("Let everyone send
     * messages"), in which case the composer is disabled and nothing is posted.
     */
    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        try {
            await this.openChatPanel(page);
            const found = await findElementWithFallback(
                page,
                [...MEET_SELECTORS.chatInput],
                {
                    intent: 'Google Meet in-call messages compose box (textarea labelled "Send a message")',
                    platform: 'GOOGLE_MEET',
                    step: 'meet.chat.input',
                },
                { maxRetries: 6, delayMs: 500 },
            );
            if (!found) {
                log.warn('Could not locate the Meet chat input — skipping message post (non-fatal).');
                return;
            }
            const disabled = await found.element
                .evaluate((el) => (el as HTMLTextAreaElement).disabled || el.getAttribute('aria-disabled') === 'true')
                .catch(() => false);
            if (disabled) {
                if (!this.chatUnavailableLogged) {
                    this.chatUnavailableLogged = true;
                    log.warn('The Meet chat composer is disabled (the host may have turned chat off) — not posting.');
                }
                return;
            }
            const isTextarea = await found.element.evaluate((el) => el.tagName === 'TEXTAREA').catch(() => false);
            const composerText = () =>
                found.element
                    .evaluate((el) => ((el as HTMLTextAreaElement).value ?? el.textContent ?? '').trim())
                    .catch(() => '');
            for (const message of messages) {
                // The whole message goes in at once. Typing it key by key costs a
                // DevTools round trip per character, which made a long message
                // crawl in visibly; keyboard.insertText is one input event, which
                // a textarea and a contenteditable composer both take.
                if (isTextarea) {
                    await found.element.fill(message).catch(() => undefined);
                } else {
                    await found.element.evaluate((el) => (el as HTMLElement).focus()).catch(() => undefined);
                    await page.keyboard.insertText(message);
                }
                const head = message.slice(0, Math.min(20, message.length));
                if (!(await composerText()).includes(head)) {
                    log.info('Composer did not take the message in one step — typing it.');
                    await found.element.evaluate((el) => (el as HTMLElement).focus()).catch(() => undefined);
                    await page.keyboard.type(message, { delay: 0 });
                }
                await found.element.press('Enter');
                await new Promise((r) => setTimeout(r, 300));
                const left = await found.element
                    .evaluate((el) => ((el as HTMLTextAreaElement).value ?? el.textContent ?? '').trim())
                    .catch(() => '');
                if (left) {
                    await this.clickIfPresent(page, MEET_SELECTORS.chatSend, 'chat send');
                    await new Promise((r) => setTimeout(r, 300));
                }
            }
            log.info('Sent messages:', messages);
        } catch (e) {
            log.warn('sendMessages failed (non-fatal — VP remains in meeting):', e);
        }
    }

    /**
     * New chat messages since the last read, each once.
     *
     * Keyed on Meet's message id, so a message is read once however often the
     * list re-renders, and the same command typed twice is read twice. The
     * sender is taken from the header of the message's group; when no group
     * matches, or its header does not read as one name and one time, the
     * sender is null rather than borrowed from a neighbouring message.
     */
    private async readNewChatMessages(page: Page): Promise<MeetChatReading[]> {
        const raw: Array<{ header: string | null; text: string }> = await page.evaluate(
            (sels: { message: string; group: string }) => {
                const w = window as any;
                const seen: Set<string> = (w.__lmaMeetSeen ||= new Set<string>());
                const out: Array<{ header: string | null; text: string }> = [];
                for (const node of Array.from(document.querySelectorAll(sels.message))) {
                    // A message nested in another (a reply or quote) is read as part of it.
                    if (node.parentElement?.closest(sels.message)) continue;
                    const text = ((node as HTMLElement).innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                    // Not marked seen until it has text, so a row filled in by a
                    // later render is read once it is complete.
                    if (!text || text.length > 2000) continue;
                    const key = node.getAttribute('data-message-id') || `text:${text}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const group = node.closest(sels.group);
                    let header: string | null = null;
                    if (group) {
                        const parts: string[] = [];
                        const walker = document.createTreeWalker(group, NodeFilter.SHOW_TEXT);
                        for (let t = walker.nextNode(); t; t = walker.nextNode()) {
                            if (t.parentElement?.closest(sels.message)) continue;
                            const piece = (t.textContent || '').trim();
                            if (piece) parts.push(piece);
                        }
                        header = parts.join(' ');
                    }
                    out.push({ header, text });
                }
                return out;
            },
            { message: MEET_IN_PAGE_SELECTORS.chatMessage, group: MEET_IN_PAGE_SELECTORS.chatGroup },
        );
        return raw.map(({ header, text }) => ({ sender: senderFromMeetGroupHeader(header), text }));
    }

    private async handleIncomingMessage(page: Page, sender: string | null, message: string): Promise<void> {
        if (isOwnMeetName(sender, this.ownIdentities()) || isOwnMeetMessage(message)) return;
        // The consent flow gets no sender on Meet. The sender reading has not
        // been confirmed against a live meeting, and a consent recorded for the
        // wrong person is worse than one not recorded: so, as on the Teams
        // browser join, a consent reply cannot be attributed (objections still
        // stop capture), and `sendersHidden` below says so.
        if (await meetingConsent.handleChat(null, message)) return;
        const assistantToggle = parseAssistantChatCommand(message);
        const chatQuestion = assistantToggle ? null : parseChatQuestion(message);
        // Not a branch of the chain below, so the question is still recorded as
        // meeting chat.
        if (chatQuestion && this.freshToggle(message, sender)) {
            void startChatAnswer(chatQuestion, sender, 'google-meet', (messages) => this.sendMessages(page, messages));
        }
        if (matchesEndCommand(message)) {
            if (this.endHandled) return;
            this.endHandled = true;
            log.info(`LMA Virtual Participant has been asked to leave by ${sender || 'a participant'}: ${JSON.stringify(message)}`);
            details.start = false;
            // The goodbye goes out before the exit is signalled, since teardown
            // closes the page; bounded so a missing composer cannot hold the exit.
            await Promise.race([
                this.sendMessages(page, exitMessagesFor(sender)),
                new Promise((r) => setTimeout(r, 8000)),
            ]).catch((e) => log.warn('Goodbye message send failed (non-fatal):', e));
            this.requestEnd({
                reason: 'end-command',
                trigger: 'chat',
                requestedBy: sender,
                matchedMessage: message,
            });
        } else if (assistantToggle) {
            if (!this.freshToggle(message, sender)) return;
            await this.sendMessages(page, await applyAssistantChatCommand(voiceAssistant, assistantToggle, sender));
        } else if (matchesChatCommand(message, details.helpCommand)) {
            if (!this.freshToggle(message, sender)) return;
            log.info('[chat-help] posting the chat command list');
            await this.sendMessages(page, chatHelpMessages(voiceAssistant));
        } else if (details.start && matchesChatCommand(message, details.pauseCommand)) {
            details.start = false;
            log.info(details.pauseMessages[0]);
            await this.sendMessages(page, details.pauseMessages);
        } else if (!details.start && matchesChatCommand(message, details.startCommand)) {
            details.start = true;
            log.info(details.startMessages[0]);
            await this.sendMessages(page, details.startMessages);
            transcriptionService.startTranscription();
        } else if (details.start) {
            const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
            const formatted = sender ? `[${timestamp}] ${sender}: ${message}` : `[${timestamp}] ${message}`;
            details.messages.push(formatted);
            log.info('New message:', formatted);
        }
    }

    // ------------------------------------------------------------- monitors

    /**
     * The participant tiles, for the active-speaker poll. `withClasses` adds the
     * class names found in each tile, for the one diagnostic line; it is left
     * off the once-a-second reads.
     */
    /**
     * Once per meeting, at debug level: the structure of the first two tiles
     * (tag, class, data-* and aria attributes, and leaf text), so a live run can
     * confirm which element holds the name and which shows the speaking state.
     */
    private async logTileStructure(page: Page): Promise<void> {
        const outline = await page
            .evaluate((tileSel: string) => {
                const describe = (el: Element, depth: number): string[] => {
                    if (depth > 9) return [];
                    const attrs = Array.from(el.attributes)
                        .filter((a) => a.name === 'class' || a.name.startsWith('data-') || a.name.startsWith('aria-') || a.name === 'role')
                        .map((a) => `${a.name}="${a.value.slice(0, 60)}"`)
                        .join(' ');
                    const own = Array.from(el.childNodes)
                        .filter((n) => n.nodeType === Node.TEXT_NODE)
                        .map((n) => (n.textContent || '').trim())
                        .filter(Boolean)
                        .join(' ')
                        .slice(0, 40);
                    const line = `${'  '.repeat(depth)}<${el.tagName.toLowerCase()} ${attrs}>${own ? ` "${own}"` : ''}`;
                    return [line, ...Array.from(el.children).flatMap((c) => describe(c, depth + 1))];
                };
                return Array.from(document.querySelectorAll(tileSel))
                    .slice(0, 2)
                    .map((t) => describe(t, 0).slice(0, 120).join('\n'));
            }, MEET_IN_PAGE_SELECTORS.participantTile)
            .catch(() => [] as string[]);
        outline.forEach((tile, i) => log.debug(`[meet] tile ${i} structure:\n${tile}`));
    }

    private async readTiles(page: Page, withClasses = false): Promise<Array<MeetTileReading & { classes?: string }>> {
        return page
            .evaluate(
                (sels: { tile: string; names: readonly string[]; speaking: readonly string[]; withClasses: boolean }) => {
                    const out: Array<{ name: string | null; speaking: boolean; classes?: string }> = [];
                    const tiles = Array.from(document.querySelectorAll(sels.tile)).slice(0, 49);
                    for (const tile of tiles) {
                        const r = (tile as HTMLElement).getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        let name: string | null = null;
                        for (const sel of sels.names) {
                            const el = tile.querySelector(sel);
                            const t = (el?.getAttribute('data-self-name') || el?.textContent || '').trim();
                            if (t) {
                                name = t;
                                break;
                            }
                        }
                        const speaking = sels.speaking.some((sel) => {
                            const el = tile.querySelector(sel) as HTMLElement | null;
                            return !!el && el.offsetParent !== null;
                        });
                        if (!sels.withClasses) {
                            out.push({ name, speaking });
                            continue;
                        }
                        // For the one-off diagnostic line: the class names in the
                        // tile, so a live run can confirm or replace the list.
                        const classes = Array.from(new Set(
                            Array.from(tile.querySelectorAll('[class]'))
                                .slice(0, 60)
                                .flatMap((el) => Array.from(el.classList)),
                        )).slice(0, 40).join(' ');
                        out.push({ name, speaking, classes });
                    }
                    return out;
                },
                {
                    tile: MEET_IN_PAGE_SELECTORS.participantTile,
                    names: MEET_IN_PAGE_SELECTORS.tileName,
                    speaking: MEET_IN_PAGE_SELECTORS.speakingIndicator,
                    withClasses,
                },
            )
            .catch(() => []);
    }

    /** One reading of the People count, corroborated by the Leave call button when absent. */
    private async readAttendees(page: Page): Promise<AttendeeReading> {
        try {
            const raw = await page.evaluate(
                (sels: { people: readonly string[]; leaveCall: readonly string[] }) => {
                    const visible = (el: Element | null): boolean => {
                        if (!el) return false;
                        const r = (el as HTMLElement).getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const inMeeting = sels.leaveCall.some((sel) => visible(document.querySelector(sel)));
                    for (const sel of sels.people) {
                        const el = document.querySelector(sel);
                        if (el) return { text: el.textContent || '', label: el.getAttribute('aria-label') || '', inMeeting };
                    }
                    return { text: null, label: null, inMeeting };
                },
                { people: MEET_IN_PAGE_SELECTORS.peopleButton, leaveCall: MEET_IN_PAGE_SELECTORS.leaveCall },
            );
            const count = parseMeetParticipantCount(raw.text, raw.label);
            if (count !== null) return { state: 'OK', count };
            return raw.inMeeting ? { state: 'BADGE_MISSING_IN_MEETING' } : { state: 'BADGE_MISSING' };
        } catch {
            return { state: 'BADGE_MISSING' };
        }
    }

    // ------------------------------------------------------------ the join

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        // Meet has no credentialled sign-in phase, so the Simli avatar comes up now.
        if (opts.prepareAvatar) await opts.prepareAvatar();
        // The AI dialog watchdog runs for the whole meeting (dialog-watchdog.ts):
        // a recording notice is acknowledged, and a CAPTCHA or sign-in wall is
        // raised as MANUAL_ACTION_REQUIRED for the user to clear in the live view.
        startDialogWatchdog(page, { platform: 'GOOGLE_MEET' });
        // A "Leave site?" beforeunload prompt must never hold the page.
        page.on('dialog', async (dialog: any) => {
            try {
                await dialog.dismiss();
            } catch {
                /* already handled, or the page closed */
            }
        });

        const substep = async (message: string): Promise<void> => {
            if (!details.invite.virtualParticipantId) return;
            try {
                await createStatusManager(details.invite.virtualParticipantId).setJoiningSubstep(message);
            } catch {
                /* best-effort progress */
            }
        };

        const meeting = parseGoogleMeetMeeting(details.invite.meetingId);
        if (!meeting) {
            log.warn('The meeting ID is not a Google Meet code or meet.google.com link — not joining.');
            return { reason: 'never-joined', trigger: 'pre-join:meet-invalid-code' };
        }

        await substep('Opening the Google Meet page…');
        try {
            log.info(`Opening Google Meet (${meeting.code ?? 'link without a meeting code'}).`);
            await gotoMeetingPage(page, meeting.url, MEETING_HOST_PATTERNS.googleMeet, 'meet-join');
        } catch {
            log.warn('Your scribe was unable to open the Google Meet page.');
            return { reason: 'never-joined', trigger: 'pre-join:goto-failed' };
        }
        await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => undefined);
        await this.passConsentInterstitial(page);

        // Meet decides whether this browser may join before showing a name field,
        // so look for the name field and the refusal page together.
        await substep('Loading the Google Meet join screen…');
        log.info('Entering name.');
        let nameField: ElementHandle<Element> | null = null;
        for (let attempt = 0; attempt < 45 && !nameField; attempt++) {
            const reading = await this.readPage(page);
            if (isMeetRefusalRedirect(reading.url)) {
                log.warn(`Google Meet sent the VP to ${reading.url} — it refused this browser as a guest.`);
                return { reason: 'never-joined', trigger: 'pre-join:meet-refused' };
            }
            const screen = classifyMeetScreenText(reading.text);
            const exit = meetPreJoinExit(screen?.screen ?? null);
            if (exit) {
                log.warn(`Google Meet showed "${screen?.phrase}" before the join — not joining (${exit.trigger}).`);
                return exit;
            }
            for (const selector of MEET_SELECTORS.nameInput) {
                const handle = await page.$(selector).catch(() => null);
                if (handle && (await handle.isVisible().catch(() => false))) {
                    nameField = handle;
                    break;
                }
            }
            // A profile signed in to Google has no name field: its join button
            // is there instead, and the join goes on without one.
            if (!nameField && reading.joinButtonVisible) break;
            if (!nameField) await new Promise((r) => setTimeout(r, 1000));
        }
        if (!nameField) {
            const reading = await this.readPage(page);
            if (!reading.joinButtonVisible) {
                // Neither a name field nor a join button: one AI look before giving up.
                const found = await findElementWithFallback(
                    page,
                    [],
                    {
                        intent: 'Google Meet pre-join screen guest name input under "What\'s your name?"',
                        platform: 'GOOGLE_MEET',
                        step: 'meet.join.name',
                        useScreenshot: true,
                    },
                    { maxRetries: 1, delayMs: 0 },
                );
                if (!found) {
                    log.warn('Could not locate the Google Meet name field or join button — aborting the join.');
                    return { reason: 'never-joined', trigger: 'pre-join:no-name-input' };
                }
                nameField = found.element;
            } else {
                log.info('No guest name field (the profile is signed in to Google) — joining with the account name.');
            }
        }
        // Meet shows a "Sign in with your Google account … Got it" callout beside
        // the guest name field. It is a tip, not a sign-in wall; dismissing it
        // before the dialog watchdog reads the page keeps the VP from being
        // marked as needing a sign-in.
        await this.dismissTips(page);
        if (nameField) {
            // fill() replaces whatever a restored profile pre-filled, so the name
            // is never doubled; checked, and typed once by keyboard if it did not land.
            await nameField.fill(details.scribeIdentity).catch(() => undefined);
            const typed = await nameField.evaluate((el: Element) => (el as HTMLInputElement).value || '').catch(() => '');
            if (typed !== details.scribeIdentity) {
                log.warn(`Name field holds "${typed}" after fill — clearing and typing.`);
                await nameField.evaluate((el: Element) => {
                    (el as HTMLInputElement).value = '';
                });
                await humanType(page, nameField, details.scribeIdentity);
            }
        }

        await substep('Setting up audio and video…');
        await this.setPrejoinDevices(page);

        log.info('Clicking join button.');
        const joinRes = await findElementWithFallback(
            page,
            [...MEET_SELECTORS.joinButton],
            {
                intent: 'Google Meet pre-join primary button that asks to join or joins the call ("Ask to join" or "Join now")',
                platform: 'GOOGLE_MEET',
                step: 'meet.join.joinButton',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!joinRes) {
            const screen = classifyMeetScreenText((await this.readPage(page)).text);
            const exit = meetPreJoinExit(screen?.screen ?? null);
            if (exit) return exit;
            log.warn('Could not locate the Google Meet join button — aborting.');
            return { reason: 'never-joined', trigger: 'pre-join:no-join-button' };
        }
        const joinLabel = ((await joinRes.element.textContent().catch(() => '')) || '').replace(/\s+/g, ' ').trim();
        log.info(`Clicking "${joinLabel || 'join'}" (${joinRes.source} selector ${joinRes.selector}).`);
        await humanClick(page, joinRes.element);
        let lastJoinClick = Date.now();
        let joinClicks = 1;

        // The admission wait. A fast DOM check every 1.5s, the AI join-state
        // check every 30s as a second opinion, and the deadline extended while a
        // manual action (a CAPTCHA) is being solved in the live view.
        await substep('Waiting to be admitted to the meeting…');
        const POLL_INTERVAL_MS = 1500;
        const startWait = Date.now();
        const baseDeadline = startWait + details.waitingTimeout;
        const MANUAL_ACTION_GRACE_MS = 5 * 60 * 1000;
        const AI_CHECK_INTERVAL_MS = 30_000;
        let lastAiCheck = Date.now();
        let lastProgressBump = Date.now();
        let waitingLogged = false;
        const sm = details.invite.virtualParticipantId ? createStatusManager(details.invite.virtualParticipantId) : null;
        let admitted = false;
        while (!admitted) {
            if (page.isClosed()) return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
            const reading = await this.readPage(page);
            if (reading.inMeeting) {
                admitted = true;
                break;
            }
            if (isMeetRefusalRedirect(reading.url)) {
                log.warn('Google Meet sent the VP to its product page — it refused this browser as a guest.');
                return { reason: 'never-joined', trigger: 'pre-join:meet-refused' };
            }
            const screen = classifyMeetScreenText(reading.text);
            const exit = meetPreJoinExit(screen?.screen ?? null);
            if (exit) {
                log.warn(`Google Meet showed "${screen?.phrase}" while asking to join (${exit.trigger}).`);
                return exit;
            }
            if (screen?.screen === 'waiting' && !waitingLogged) {
                waitingLogged = true;
                log.info(`Asked to join; Meet shows "${screen.phrase}".`);
                await substep('Asked to join — waiting for someone in the meeting to let the participant in…');
            }
            // A join click that did not register leaves the button on screen with
            // no waiting text; it is clicked again, at most twice more.
            if (reading.joinButtonVisible && screen?.screen !== 'waiting' && Date.now() - lastJoinClick > 15_000 && joinClicks < 3) {
                const again = await findElementWithFallback(
                    page,
                    [...MEET_SELECTORS.joinButton],
                    { intent: 'Google Meet "Ask to join" / "Join now" button', platform: 'GOOGLE_MEET', step: 'meet.join.joinButton' },
                    { maxRetries: 2, delayMs: 300 },
                );
                if (again) {
                    joinClicks += 1;
                    lastJoinClick = Date.now();
                    log.info(`The join button is still showing — clicking it again (${joinClicks}/3).`);
                    await humanClick(page, again.element);
                }
            }
            if (isResolverEnabled() && Date.now() - lastAiCheck > AI_CHECK_INTERVAL_MS) {
                lastAiCheck = Date.now();
                try {
                    const verdict = await classifyJoinState(page, { platform: 'GOOGLE_MEET' });
                    if (verdict) {
                        log.info(`[meet] AI join-state check: ${verdict.state} — ${verdict.reason}`);
                        if (verdict.state === 'in-meeting') {
                            admitted = true;
                            break;
                        }
                        if (verdict.state === 'error') {
                            return { reason: 'never-joined', trigger: 'pre-join:ai-error-screen' };
                        }
                    }
                } catch (e) {
                    log.warn('[meet] AI join-state check failed (non-fatal):', e);
                }
            }
            if (Date.now() - lastProgressBump > 20_000) {
                lastProgressBump = Date.now();
                const secs = Math.round((Date.now() - startWait) / 1000);
                const current = sm ? await sm.getCurrentStatus().catch(() => null) : null;
                if (current !== 'MANUAL_ACTION_REQUIRED') {
                    await substep(`Waiting to be admitted to the meeting… (${secs}s — someone in the meeting may need to admit the participant)`);
                }
            }
            let extended = false;
            if (sm && Date.now() > baseDeadline) {
                const current = await sm.getCurrentStatus().catch(() => null);
                extended = current === 'MANUAL_ACTION_REQUIRED' && Date.now() < baseDeadline + MANUAL_ACTION_GRACE_MS;
            }
            if (Date.now() > baseDeadline && !extended) {
                log.info('Your scribe was not admitted into the meeting.');
                return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
            }
            await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        }

        // In the meeting. JOINED first: everything after it is best effort and
        // must not turn a successful join into a failure.
        if (sm) await sm.setJoined();
        log.info('Successfully joined Google Meet meeting');

        await this.dismissTips(page);
        await substep('In the meeting — opening chat panel…');
        await this.openChatPanel(page);
        await substep('In the meeting — posting introduction message…');
        log.info('Sending introduction messages.');
        await this.sendMessages(page, details.introMessages);
        await substep('In the meeting — listening for participants and transcript…');

        const timers: NodeJS.Timeout[] = [];
        const stopTimers = () => timers.forEach((t) => clearInterval(t));
        page.once('close', stopTimers);

        try {
            // Attendee watchdog: Teams' debounced decision (decideAttendeeAction),
            // with its default bounds, read from the People count. An unreadable
            // count with the Leave call button on screen is not a strike.
            const watchdogConfig = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
            const watchdogState: AttendeeWatchdogState = { consecutiveLonely: 0, consecutiveMissing: 0, suppressedMissing: 0, unreadableRun: 0 };
            let attendeePollInFlight = false;
            log.info(`Listening for attendee changes (poll ${watchdogConfig.pollMs}ms, alone x${watchdogConfig.pollsBeforeEnd}).`);
            timers.push(setInterval(async () => {
                if (page.isClosed() || attendeePollInFlight) return;
                attendeePollInFlight = true;
                try {
                    const reading = await this.readAttendees(page);
                    if (reading.state === 'OK') meetingConsent.noteAttendeeCount(reading.count);
                    const decision = decideAttendeeAction(reading, watchdogState, watchdogConfig);
                    log.info(`DEBUG: Meet attendee reading ${JSON.stringify(reading)} lonely=${watchdogState.consecutiveLonely} missing=${watchdogState.consecutiveMissing}`);
                    if (decision.action === 'end') {
                        log.info(`LMA Virtual Participant is leaving: ${decision.detail}`);
                        details.start = false;
                        this.requestEnd({ reason: decision.reason, trigger: decision.trigger });
                    }
                } finally {
                    attendeePollInFlight = false;
                }
            }, watchdogConfig.pollMs));

            // Active speaker, from the participant tiles. While the voice
            // assistant is talking the VP itself is the speaker.
            let lastMeetingSpeaker: string | null = null;
            let lastReported: string | null = null;
            const report = async (speaker: string) => {
                if (!speaker || speaker === lastReported) return;
                lastReported = speaker;
                await transcriptionService.speakerChange(speaker);
            };
            if (voiceAssistant.isEnabled()) {
                const onAgentStart = () => {
                    report(details.scribeIdentity).catch(() => {});
                };
                const onAgentStop = () => {
                    if (lastMeetingSpeaker) report(lastMeetingSpeaker).catch(() => {});
                };
                agentSpeakingDetector.on('started', onAgentStart);
                agentSpeakingDetector.on('stopped', onAgentStop);
                page.once('close', () => {
                    agentSpeakingDetector.off('started', onAgentStart);
                    agentSpeakingDetector.off('stopped', onAgentStop);
                });
            }
            let tilesLogged = false;
            let speakerPollInFlight = false;
            log.info('Listening for speaker changes.');
            timers.push(setInterval(async () => {
                if (page.isClosed() || speakerPollInFlight) return;
                speakerPollInFlight = true;
                try {
                    const tiles = await this.readTiles(page, !tilesLogged);
                    if (!tilesLogged && tiles.length > 0) {
                        tilesLogged = true;
                        log.info(`[meet] participant tiles: ${JSON.stringify(tiles.slice(0, 3))}`);
                        void this.logTileStructure(page);
                    }
                    const speaker = pickMeetActiveSpeaker(tiles, this.ownIdentities(), lastMeetingSpeaker);
                    if (speaker && speaker !== lastMeetingSpeaker) {
                        lastMeetingSpeaker = speaker;
                        if (!(voiceAssistant.isEnabled() && agentSpeakingDetector.isSpeaking())) await report(speaker);
                    }
                } finally {
                    speakerPollInFlight = false;
                }
            }, 1000));

            // Chat reader. Meet keeps messages in the DOM only while the panel is
            // open, so the panel is reopened (at most every 30s) when it has closed.
            let lastReopen = 0;
            let chatPollInFlight = false;
            log.info('Listening for message changes.');
            timers.push(setInterval(async () => {
                if (page.isClosed() || chatPollInFlight) return;
                chatPollInFlight = true;
                try {
                    if (!(await this.chatInputVisible(page)) && Date.now() - lastReopen > 30_000) {
                        lastReopen = Date.now();
                        await this.openChatPanel(page);
                    }
                    for (const { sender, text } of await this.readNewChatMessages(page)) {
                        await this.handleIncomingMessage(page, sender, text);
                    }
                } catch {
                    /* the page is navigating; read again next tick */
                } finally {
                    chatPollInFlight = false;
                }
            }, 2000));
        } catch (e) {
            log.warn('In-meeting monitoring setup failed (non-fatal — VP remains in meeting):', e);
        }

        // The meeting consent flow, when the meeting runs one (meeting-consent.ts).
        // The People panel takes the chat's place in the side panel, so the chat
        // is reopened after the screenshot of it.
        attachMeetingConsent(page, {
            send: (messages) => this.sendMessages(page, messages),
            // See handleIncomingMessage: consent replies are read without a sender.
            sendersHidden: true,
            panels: toolbarPanels(page, {
                listVisible: [...MEET_SELECTORS.peopleList],
                open: [...MEET_SELECTORS.peopleButton],
                close: [],
                reopenChat: () => this.openChatPanel(page),
            }),
            leave: (objector) => {
                details.start = false;
                this.requestEnd({ reason: 'consent-objection', trigger: 'consent-objection', requestedBy: objector });
            },
        });

        // Start transcription if enabled (LMA behavior).
        if (details.start) {
            log.info(details.startMessages[0]);
            await this.sendMessages(page, details.startMessages);
            transcriptionService.startTranscription();
        }

        log.info('Waiting for meeting end.');
        // Meet's own end screens, read only once the Leave call button is gone
        // (meetInMeetingExit), and a navigation away from Meet.
        const meetEnded = new Promise<ExitInfo>((resolve) => {
            let inFlight = false;
            const handle = setInterval(async () => {
                if (inFlight) return;
                inFlight = true;
                try {
                    if (page.isClosed()) {
                        clearInterval(handle);
                        resolve({ reason: 'page-closed', trigger: 'navigated-away' });
                        return;
                    }
                    const reading = await this.readPage(page);
                    if (reading.inMeeting) return;
                    let host = '';
                    try {
                        host = new URL(reading.url).hostname.toLowerCase();
                    } catch {
                        /* about:blank and the like */
                    }
                    if (host !== MEET_HOST) {
                        clearInterval(handle);
                        resolve({ reason: 'page-closed', trigger: 'navigated-away' });
                        return;
                    }
                    const screen = classifyMeetScreenText(reading.text);
                    const exit = meetInMeetingExit(screen?.screen ?? null);
                    if (exit) {
                        log.info(`Google Meet showed "${screen?.phrase}" — leaving (${exit.trigger}).`);
                        clearInterval(handle);
                        resolve(exit);
                    }
                } finally {
                    inFlight = false;
                }
            }, 2000);
            timers.push(handle);
        });
        const meetingTimeout = new Promise<ExitInfo>((resolve) => {
            timers.push(setTimeout(() => resolve({ reason: 'meeting-timeout', trigger: 'meetingTimeout' }), details.meetingTimeout));
        });
        try {
            const exitInfo = await Promise.race([this.endRequested, meetEnded, meetingTimeout]);
            log.info(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
            return exitInfo;
        } finally {
            details.start = false;
            stopTimers();
        }
    }
}
