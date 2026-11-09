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
import { noteSentChat } from './meeting-chat.js';
import { meetingChat } from './meeting-chat-runtime.js';
import { chatAddressing } from './chat-addressing.js';
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
    chooseMeetSpeaker,
    classifyMeetScreenText,
    cleanMeetParticipantName,
    isMeetRefusalRedirect,
    isOwnMeetName,
    meetTileSpeaking,
    parseGoogleMeetMeeting,
    parseMeetParticipantCount,
    senderFromMeetGroupHeader,
    readMeetChatInPage,
    MeetChatRaw,
    MEET_CHAT_HEADER_MAX_LEVELS,
    timeFromMeetGroupHeader,
} from './google-meet-page.js';
import { createLogger } from './logger.js';

/** Fresh page loads tried after Meet refuses the guest before the join. */
export const MEET_REFUSAL_RETRIES = 4;
/** The pause before each of those retries. */
export const MEET_REFUSAL_BACKOFF_MS = [4_000, 8_000, 15_000, 20_000];

/** One tile as the speaker poll uses it: the reading, its id, and the level indicator's classes. */
interface MeetTile extends MeetTileReading {
    id: string;
    indicatorClasses: string | null;
    classes?: string;
}

/** Tile speaking-state changes logged per meeting; the rest are counted, not logged. */
const MAX_LOGGED_SPEAKING_CHANGES = 50;

/** How long `logTileActivity` counts before it reports and stops. */
const TILE_ACTIVITY_WINDOW_MS = 45_000;

/** The page state `logTileActivity` keeps on `window` for its counts. */
interface MeetActivityWindow {
    __lmaMeetActivity?: Record<string, Record<string, number>>;
    __lmaMeetActivityObserver?: MutationObserver;
}

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
    /** Meet's data-message-id. */
    id: string | null;
    /** The time the message's group header shows. */
    time: string | null;
    links: string[];
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
        // So the meeting chat capture knows these when it reads them back.
        noteSentChat(messages);
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
            const composerText = () =>
                found.element
                    .evaluate((el) => ((el as HTMLTextAreaElement).value ?? el.textContent ?? '').trim())
                    .catch(() => '');
            for (const message of messages) {
                // The whole message goes in at once, from inside the page. The
                // browser runs with CloakBrowser's humanize layer, which turns
                // Playwright's fill(), type() and insertText() into per-key
                // typing; a ~250-character message then crawls in for most of a
                // minute. execCommand('insertText') is one input event the
                // composer handles like a paste, as on Zoom (zoom.ts).
                await found.element
                    .evaluate((el, text) => {
                        const node = el as HTMLTextAreaElement | HTMLElement;
                        node.focus();
                        if (node instanceof HTMLTextAreaElement) {
                            node.select();
                        } else {
                            const sel = window.getSelection();
                            if (sel) {
                                const range = document.createRange();
                                range.selectNodeContents(node);
                                sel.removeAllRanges();
                                sel.addRange(range);
                            }
                        }
                        if (document.execCommand('insertText', false, text as string)) return;
                        // execCommand refused: set the value the way a framework
                        // sees it, through the native setter and an input event.
                        if (node instanceof HTMLTextAreaElement) {
                            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
                            setter?.call(node, text as string);
                            node.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    }, message)
                    .catch(() => undefined);
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
            log.info(`Sent ${messages.length} chat message(s).`);
        } catch (e) {
            log.warn('sendMessages failed (non-fatal — VP remains in meeting):', e);
        }
    }

    /**
     * New chat messages since the last read, each once.
     *
     * Keyed on Meet's message id, so a message is read once however often the
     * list re-renders, and the same command typed twice is read twice. The
     * sender is taken from the header of the message's group
     * (readMeetChatInPage); when no header is found, or it does not read as one
     * name and one time, the sender is null rather than borrowed from a
     * neighbouring message.
     */
    private async readNewChatMessages(page: Page): Promise<MeetChatReading[]> {
        const raw: MeetChatRaw[] = await page.evaluate(readMeetChatInPage, {
            message: MEET_IN_PAGE_SELECTORS.chatMessage,
            group: MEET_IN_PAGE_SELECTORS.chatGroup,
            maxLevels: MEET_CHAT_HEADER_MAX_LEVELS,
        });
        return raw.map(({ header, text, id, links }) => ({
            sender: senderFromMeetGroupHeader(header),
            text,
            id: id || null,
            time: timeFromMeetGroupHeader(header),
            links,
        }));
    }

    private async handleIncomingMessage(
        page: Page,
        sender: string | null,
        rawMessage: string,
        reading: Partial<MeetChatReading> = {},
    ): Promise<void> {
        if (isOwnMeetName(sender, this.ownIdentities()) || isOwnMeetMessage(rawMessage)) return;
        // Which LMA VP the message is for, when there are several
        // (chat-addressing.ts). One for another VP, or the bare "LMA" with
        // several here, is meeting chat only.
        const route = chatAddressing.route(rawMessage, sender);
        if (!route.act) {
            meetingChat.offer({
                platform: 'google-meet',
                text: rawMessage,
                commandText: route.text,
                sender,
                id: reading.id ?? null,
                platformTime: reading.time ?? null,
                audience: 'everyone',
                links: reading.links,
            });
            return;
        }
        // Read with its address reduced to "LMA".
        const message = route.text;
        // A consent reply, or "LMA start" while consent is withheld, under the
        // sender read from the message's group header, as on Zoom and Chime. A
        // header that spans two groups gives no sender, and a consent reply
        // without one is not recorded (senderFromMeetGroupHeader).
        if (await meetingConsent.handleChat(sender, message)) return;
        const assistantToggle = parseAssistantChatCommand(message);
        const chatQuestion = assistantToggle ? null : parseChatQuestion(message);
        // Not a branch of the chain below, so the question is still recorded as
        // meeting chat.
        if (chatQuestion && this.freshToggle(message, sender)) {
            void startChatAnswer(chatQuestion, sender, 'google-meet', async (messages, answered) => {
                await this.sendMessages(page, messages);
                // The meeting assistant's answer, not a notice, is added to the
                // transcript when the question was (meeting-chat.ts).
                if (answered) {
                    meetingChat.offerAnswer(
                        { platform: 'google-meet', text: rawMessage, sender, id: reading.id ?? null },
                        messages,
                    );
                }
            });
        }
        if (matchesEndCommand(message)) {
            if (this.endHandled) return;
            this.endHandled = true;
            log.info(`LMA Virtual Participant has been asked to leave by ${sender || 'a participant'} (message of ${String(message ?? '').length} characters)`);
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
        } else {
            // Meeting chat, for the transcript (meeting-chat.ts decides). Meet
            // has no private messages in a meeting's chat.
            meetingChat.offer({
                platform: 'google-meet',
                text: rawMessage,
                commandText: message,
                sender,
                id: reading.id ?? null,
                platformTime: reading.time ?? null,
                audience: 'everyone',
                links: reading.links,
            });
        }
    }

    // ------------------------------------------------------------- monitors

    /**
     * Once per meeting, at debug level: which elements inside each tile change
     * attributes, counted per tile, with the element's tag, class, jscontroller
     * and the attribute. The element whose changes follow a participant's
     * speech is the speaking indicator; this is how a live run shows which one
     * it is when Meet changes its page. The observer watches every attribute
     * in the page, so it runs for one window only: it reports after
     * `TILE_ACTIVITY_WINDOW_MS`, or when the meeting ends (`ended` resolves)
     * if that is sooner, and is then disconnected and its counts deleted, so
     * it does no work for the rest of the meeting. (The INFO line for a tile's
     * speaking-state change comes from the speaker poll, not from here.)
     */
    private async logTileActivity(page: Page, ended: Promise<void>): Promise<void> {
        const started = await page
            .evaluate((tileSel: string) => {
                const w = window as unknown as MeetActivityWindow;
                if (w.__lmaMeetActivity) return false;
                const activity: Record<string, Record<string, number>> = {};
                w.__lmaMeetActivity = activity;
                const observer = new MutationObserver((mutations) => {
                    const counts = w.__lmaMeetActivity;
                    if (!counts) return;
                    for (const m of mutations) {
                        const el = m.target as Element;
                        const tile = el.closest?.(tileSel);
                        if (!tile) continue;
                        const id = (tile.getAttribute('data-participant-id') || '?').split('/').pop() || '?';
                        const cls = typeof (el as HTMLElement).className === 'string' ? (el as HTMLElement).className : '';
                        const key = `${el.tagName.toLowerCase()}.${cls.split(' ').slice(0, 3).join('.')}` +
                            `[jsc=${el.getAttribute('jscontroller') || ''}] @${m.attributeName}`;
                        const perTile = (counts[id] ??= {});
                        perTile[key] = (perTile[key] ?? 0) + 1;
                    }
                });
                observer.observe(document.body, { subtree: true, attributes: true });
                w.__lmaMeetActivityObserver = observer;
                return true;
            }, MEET_IN_PAGE_SELECTORS.participantTile)
            .catch(() => false);
        if (!started) return;

        // The counts, with the observer removed.
        const take = () =>
            page
                .evaluate(() => {
                    const w = window as unknown as MeetActivityWindow;
                    const counts = w.__lmaMeetActivity ?? {};
                    w.__lmaMeetActivityObserver?.disconnect();
                    delete w.__lmaMeetActivityObserver;
                    delete w.__lmaMeetActivity;
                    return counts;
                })
                .catch(() => ({}) as Record<string, Record<string, number>>);
        const report = (activity: Record<string, Record<string, number>>, span: string) => {
            for (const [tile, counts] of Object.entries(activity)) {
                const top = Object.entries(counts)
                    .sort((a, b) => b[1] - a[1])
                    .slice(0, 15)
                    .map(([k, n]) => `${n}× ${k}`)
                    .join(' | ');
                log.debug(`[meet] tile ${tile} activity ${span}: ${top}`);
            }
        };

        let timer: NodeJS.Timeout | undefined;
        const endedFirst = await Promise.race([
            ended.then(() => true),
            new Promise<boolean>((resolve) => {
                timer = setTimeout(() => resolve(false), TILE_ACTIVITY_WINDOW_MS);
                timer.unref?.();
            }),
        ]);
        clearTimeout(timer);
        report(await take(), endedFirst ? 'until the meeting ended' : `over ${TILE_ACTIVITY_WINDOW_MS / 1000} s`);
    }

    /**
     * Once per meeting, at debug level: the structure of the first two tiles
     * (tag, class, jscontroller, data-* and aria attributes, and leaf text), so
     * a live run can confirm which element holds the name and which shows the
     * speaking state.
     */
    private async logTileStructure(page: Page): Promise<void> {
        const outline = await page
            .evaluate((tileSel: string) => {
                const describe = (el: Element, depth: number): string[] => {
                    if (depth > 12) return [];
                    const attrs = Array.from(el.attributes)
                        .filter((a) =>
                            a.name === 'class' ||
                            a.name === 'jscontroller' ||
                            a.name.startsWith('data-') ||
                            a.name.startsWith('aria-') ||
                            a.name === 'role')
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
                    .map((t) => describe(t, 0).slice(0, 160).join('\n'));
            }, MEET_IN_PAGE_SELECTORS.participantTile)
            .catch(() => [] as string[]);
        outline.forEach((tile, i) => log.debug(`[meet] tile ${i} structure:\n${tile}`));
    }

    /**
     * The participant tiles, for the active-speaker poll, with whether each
     * reads as speaking (`meetTileSpeaking`) and the level indicator's class
     * list, for the line logged when a tile's state changes. `withClasses` adds
     * the class names found in each tile, for the one diagnostic line; it is
     * left off the once-a-second reads.
     */
    private async readTiles(page: Page, withClasses = false): Promise<MeetTile[]> {
        const raw = await page
            .evaluate(
                (sels: {
                    tile: string;
                    names: readonly string[];
                    speaking: readonly string[];
                    indicator: string;
                    level: string;
                    silent: string;
                    withClasses: boolean;
                }) => {
                    // Install, once, an observer that stamps a tile when its
                    // audio indicator shows the participant heard while visible:
                    // the jscontroller indicator on any class change, the level
                    // indicator when its class list changes to one without the
                    // silent class.
                    const w = window as unknown as { __lmaMeetHeard?: Record<string, number> };
                    if (!w.__lmaMeetHeard) {
                        w.__lmaMeetHeard = {};
                        const heard = w.__lmaMeetHeard;
                        new MutationObserver((mutations) => {
                            for (const m of mutations) {
                                const el = m.target as HTMLElement;
                                if (m.attributeName !== 'class' || !el.matches) continue;
                                const level = el.matches(sels.level);
                                if (!level && !el.matches(sels.indicator)) continue;
                                if (el.offsetParent === null) continue;
                                if (level && (el.classList.contains(sels.silent) || m.oldValue === el.className)) continue;
                                const id = el.closest(sels.tile)?.getAttribute('data-participant-id');
                                if (id) heard[id] = Date.now();
                            }
                        }).observe(document.body, {
                            subtree: true,
                            attributes: true,
                            attributeFilter: ['class'],
                            attributeOldValue: true,
                        });
                    }
                    const heard = w.__lmaMeetHeard;
                    const out: Array<{
                        id: string;
                        name: string | null;
                        indicatorClasses: string | null;
                        indicatorVisible: boolean;
                        heardRecently: boolean;
                        speakingClassVisible: boolean;
                        classes?: string;
                    }> = [];
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
                        const id = tile.getAttribute('data-participant-id') || '';
                        const level = tile.querySelector(sels.level) as HTMLElement | null;
                        const reading = {
                            id,
                            name,
                            indicatorClasses: level ? (typeof level.className === 'string' ? level.className : '') : null,
                            indicatorVisible: !!level && level.offsetParent !== null,
                            heardRecently: Date.now() - (heard[id] ?? 0) < 1500,
                            speakingClassVisible: sels.speaking.some((sel) => {
                                const el = tile.querySelector(sel) as HTMLElement | null;
                                return !!el && el.offsetParent !== null;
                            }),
                        };
                        if (!sels.withClasses) {
                            out.push(reading);
                            continue;
                        }
                        // For the one-off diagnostic line: the class names in the
                        // tile, so a live run can confirm or replace the list.
                        const classes = Array.from(new Set(
                            Array.from(tile.querySelectorAll('[class]'))
                                .slice(0, 60)
                                .flatMap((el) => Array.from(el.classList)),
                        )).slice(0, 40).join(' ');
                        out.push({ ...reading, classes });
                    }
                    return out;
                },
                {
                    tile: MEET_IN_PAGE_SELECTORS.participantTile,
                    names: MEET_IN_PAGE_SELECTORS.tileName,
                    speaking: MEET_IN_PAGE_SELECTORS.speakingIndicator,
                    indicator: MEET_IN_PAGE_SELECTORS.audioIndicator,
                    level: MEET_IN_PAGE_SELECTORS.levelIndicator,
                    silent: MEET_IN_PAGE_SELECTORS.silentIndicatorClass,
                    withClasses,
                },
            )
            .catch(() => []);
        return raw.map(({ id, name, indicatorClasses, classes, ...signals }) => ({
            id,
            name,
            speaking: meetTileSpeaking({ indicatorClasses, ...signals }),
            indicatorClasses,
            ...(classes === undefined ? {} : { classes }),
        }));
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
            log.info(`Opening Google Meet (${meeting.code}).`);
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
        // Meet's guest check refuses some page loads and accepts the next one:
        // measured from this network, the same browser was refused on one load
        // of a meeting with open access and shown the name field on the next.
        // So a refusal before the join is retried with a fresh page load, a
        // few times and with a growing pause. A Meet join always starts from a
        // fresh browser profile (acquireProfile saves none for Meet), but the
        // refused loads of this session leave Google cookies behind, so from
        // the second retry on they are cleared and the reload arrives without
        // them, as the first load of the session did.
        let refusals = 0;
        const retryAfterRefusal = async (why: string): Promise<boolean> => {
            if (refusals >= MEET_REFUSAL_RETRIES) return false;
            refusals += 1;
            const pauseMs = MEET_REFUSAL_BACKOFF_MS[refusals - 1] ?? 20_000;
            log.warn(`${why} Retrying the page load (${refusals} of ${MEET_REFUSAL_RETRIES}) in ${pauseMs / 1000}s.`);
            await new Promise((r) => setTimeout(r, pauseMs));
            if (refusals >= 2) {
                await page
                    .context()
                    .clearCookies({ domain: /(^|\.)google\.com$/ })
                    .catch(() => undefined);
            }
            await gotoMeetingPage(page, meeting.url, MEETING_HOST_PATTERNS.googleMeet, 'meet-join').catch(() => undefined);
            await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => undefined);
            await this.passConsentInterstitial(page);
            return true;
        };
        for (let attempt = 0; attempt < 45 && !nameField; attempt++) {
            const reading = await this.readPage(page);
            if (isMeetRefusalRedirect(reading.url)) {
                if (await retryAfterRefusal(`Google Meet sent the VP to ${reading.url}.`)) {
                    attempt = 0;
                    continue;
                }
                log.warn(`Google Meet sent the VP to ${reading.url} — it refused this browser as a guest.`);
                return { reason: 'never-joined', trigger: 'pre-join:meet-refused' };
            }
            const screen = classifyMeetScreenText(reading.text);
            const exit = meetPreJoinExit(screen?.screen ?? null);
            if (exit?.trigger === 'pre-join:meet-refused' && (await retryAfterRefusal(`Google Meet showed "${screen?.phrase}".`))) {
                attempt = 0;
                continue;
            }
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
            // Defensive fallback: a browser signed in to Google has no name
            // field, only its join button, and the join goes on without one. A
            // Meet join starts from a fresh guest profile, so this is not
            // expected to happen; it is kept so that a join button shown without
            // a name field is used rather than treated as a missing name field.
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
                // The defensive fallback above: not expected with a fresh guest profile.
                log.info('No guest name field, only the join button (as for a browser signed in to Google) — joining without entering a name.');
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
        // Resolved when the meeting ends, for the diagnostics that report then.
        let signalMeetingEnded: () => void = () => {};
        const meetingEndedSignal = new Promise<void>((resolve) => (signalMeetingEnded = resolve));
        let tileActivity: Promise<void> = Promise.resolve();

        try {
            // Attendee watchdog: Teams' debounced decision (decideAttendeeAction),
            // with its default bounds, read from the People count. An unreadable
            // count with the Leave call button on screen is not a strike.
            const watchdogConfig = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
            const watchdogState: AttendeeWatchdogState = { consecutiveLonely: 0, consecutiveMissing: 0, suppressedMissing: 0, unreadableRun: 0 };
            let attendeePollInFlight = false;
            let lastAttendeeLine = '';
            log.info(`Listening for attendee changes (poll ${watchdogConfig.pollMs}ms, alone x${watchdogConfig.pollsBeforeEnd}).`);
            timers.push(setInterval(async () => {
                if (page.isClosed() || attendeePollInFlight) return;
                attendeePollInFlight = true;
                try {
                    const reading = await this.readAttendees(page);
                    if (reading.state === 'OK') meetingConsent.noteAttendeeCount(reading.count);
                    const decision = decideAttendeeAction(reading, watchdogState, watchdogConfig);
                    // At debug level, and only when the reading or the watchdog's
                    // counters change: this runs on every poll for the whole meeting.
                    const attendeeLine = `${JSON.stringify(reading)} lonely=${watchdogState.consecutiveLonely} missing=${watchdogState.consecutiveMissing}`;
                    if (attendeeLine !== lastAttendeeLine) {
                        lastAttendeeLine = attendeeLine;
                        log.debug(`[meet] attendee reading ${attendeeLine}`);
                    }
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
            // Each tile's last speaking state, by tile id, for the INFO line
            // logged when it changes (at most MAX_LOGGED_SPEAKING_CHANGES).
            const tileSpeaking = new Map<string, boolean>();
            let speakingChangesLogged = 0;
            const sourcesLogged = new Set<string>();
            log.info('Listening for speaker changes.');
            timers.push(setInterval(async () => {
                if (page.isClosed() || speakerPollInFlight) return;
                speakerPollInFlight = true;
                try {
                    const tiles = await this.readTiles(page, !tilesLogged);
                    // Another LMA VP's tile (chat-addressing.ts). Noted, not
                    // taken as the whole list: a large meeting shows only some
                    // of its participants as tiles.
                    for (const tile of tiles) chatAddressing.notePeer(cleanMeetParticipantName(tile.name));
                    if (!tilesLogged && tiles.length > 0) {
                        tilesLogged = true;
                        log.info(`[meet] participant tiles: ${JSON.stringify(tiles.slice(0, 3))}`);
                        void this.logTileStructure(page);
                        tileActivity = this.logTileActivity(page, meetingEndedSignal);
                    }
                    for (const tile of tiles) {
                        if (!tile.id || tileSpeaking.get(tile.id) === tile.speaking) continue;
                        const first = !tileSpeaking.has(tile.id);
                        tileSpeaking.set(tile.id, tile.speaking);
                        // A tile first seen silent is not a change worth a line.
                        if ((first && !tile.speaking) || speakingChangesLogged > MAX_LOGGED_SPEAKING_CHANGES) continue;
                        speakingChangesLogged += 1;
                        if (speakingChangesLogged > MAX_LOGGED_SPEAKING_CHANGES) {
                            log.info(`[meet] ${MAX_LOGGED_SPEAKING_CHANGES} tile speaking changes logged; not logging more`);
                            continue;
                        }
                        log.info(
                            `[meet] tile ${tile.id.split('/').pop()} (${tile.name ?? 'no name'}) `
                                + `speaking=${tile.speaking} indicator=${JSON.stringify(tile.indicatorClasses)}`,
                        );
                    }
                    const choice = chooseMeetSpeaker(tiles, this.ownIdentities(), lastMeetingSpeaker);
                    if (choice && !sourcesLogged.has(choice.source)) {
                        // Logged the first time each source decides the speaker,
                        // never per poll.
                        sourcesLogged.add(choice.source);
                        log.info(
                            choice.source === 'sole-participant'
                                ? '[meet] no tile reads as speaking; reporting the one other participant as the speaker'
                                : '[meet] the active speaker is read from the tiles',
                        );
                    }
                    const speaker = choice?.speaker ?? null;
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
                    for (const reading of await this.readNewChatMessages(page)) {
                        await this.handleIncomingMessage(page, reading.sender, reading.text, reading);
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
            // The tile diagnostics' last report; bounded, since the page may be going.
            signalMeetingEnded();
            let teardownTimer: NodeJS.Timeout | undefined;
            await Promise.race([
                tileActivity,
                new Promise((r) => {
                    teardownTimer = setTimeout(r, 3000);
                    teardownTimer.unref?.();
                }),
            ]);
            clearTimeout(teardownTimer);
        }
    }
}
