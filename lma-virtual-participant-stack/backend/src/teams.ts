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

    // Candidate selectors for the Teams in-meeting chat compose box. Spans the
    // CKEditor-based composer (.ck-editor__editable / [contenteditable] with a
    // message-y aria-label) used by the v2 client and the light-meetings client.
    // The legacy `.ck-placeholder` is only the placeholder hint span and is
    // absent once anything is typed, so it's a poor primary — keep it last.
    private static readonly CHAT_INPUT_SELECTORS = [
        '[data-tid="ckeditor"] [contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"][aria-label*="message" i]',
        'div[contenteditable="true"][data-tid="ckeditor-replyBox"]',
        '.ck-editor__editable[contenteditable="true"]',
        '.cke_editable[contenteditable="true"]',
        '.ck-placeholder',
    ];

    // Candidate selectors for the in-meeting toolbar button that OPENS the chat
    // panel, spanning the v2 client (#chat-button) and light-meetings / aria
    // variants. Teams' chat button is a toggle, so callers must pre-check that
    // the panel isn't already open before clicking (see openChatPanel).
    private static readonly CHAT_BUTTON_SELECTORS = [
        '#chat-button',
        '[data-tid="chat-button"]',
        'button[data-tid="toggle-chat"]',
        'button[aria-label*="meeting chat" i]',
        'button[aria-label*="show conversation" i]',
        'button[aria-label*="chat" i]',
    ];

    // True when the chat compose box is present AND visible — the reliable
    // "panel is open" signal. Used to make openChatPanel idempotent (the toolbar
    // button is a toggle: clicking an already-open panel would CLOSE it).
    private async chatInputVisible(page: Page): Promise<boolean> {
        return page
            .evaluate((sels: string[]) => {
                for (const s of sels) {
                    const el = document.querySelector(s) as HTMLElement | null;
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
            }, Teams.CHAT_INPUT_SELECTORS)
            .catch(() => false);
    }

    // Open the in-meeting chat panel idempotently. No-op if the compose box is
    // already visible (the toolbar button is a toggle — re-clicking would close
    // it). Otherwise locate the chat button (primary selectors → AI fallback)
    // and click ONCE, then wait briefly for the input to render. Returns whether
    // the panel ended up open. Never throws.
    private async openChatPanel(page: Page): Promise<boolean> {
        if (await this.chatInputVisible(page)) return true;
        try {
            const chatBtn = await findElementWithFallback(
                page,
                Teams.CHAT_BUTTON_SELECTORS,
                {
                    intent: 'Microsoft Teams in-meeting toolbar button that OPENS the chat / conversation panel (its label mentions "chat" or "conversation"). NOT the people/roster button, NOT a close button.',
                    platform: 'TEAMS',
                    step: 'teams.meeting.chatButton',
                    useScreenshot: true,
                },
                { maxRetries: 8, delayMs: 500 },
            );
            if (!chatBtn) {
                console.log('Could not locate Teams chat panel button — dumping toolbar DOM for diagnosis.');
                await this.dumpToolbarDom(page);
                return false;
            }
            console.log(`Opening chat panel (via ${chatBtn.source} selector: ${chatBtn.selector}).`);
            await humanClick(page, chatBtn.element);
        } catch (e) {
            console.warn('Opening chat panel failed (non-fatal):', e);
            return false;
        }
        const until = Date.now() + 4000;
        while (Date.now() < until) {
            if (await this.chatInputVisible(page)) return true;
            await new Promise((r) => setTimeout(r, 300));
        }
        return this.chatInputVisible(page);
    }

    // Diagnostic dump of the in-meeting top toolbar buttons (chat/people/etc.)
    // so we can pin the real chat-toggle selector for the light-meetings client.
    // Best-effort, never throws.
    private async dumpToolbarDom(page: Page): Promise<void> {
        try {
            const btns = await page.evaluate(() => {
                const all = Array.from(document.querySelectorAll('button, [role="button"]'));
                return all
                    .map((el) => {
                        const r = (el as HTMLElement).getBoundingClientRect();
                        return {
                            id: (el as HTMLElement).id || undefined,
                            tid: el.getAttribute('data-tid') || undefined,
                            aria: el.getAttribute('aria-label') || undefined,
                            title: el.getAttribute('title') || undefined,
                            text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30) || undefined,
                            vis: r.width > 0 && r.height > 0,
                            y: Math.round(r.y),
                        };
                    })
                    // Top-of-screen toolbar controls only (y < 200), visible.
                    .filter((b) => b.vis && b.y < 200);
            });
            console.log(`[toolbar-dom] top buttons=${JSON.stringify(btns)}`);
        } catch (e) {
            console.warn('[toolbar-dom] dump failed (non-fatal):', e);
        }
    }

    // Diagnostic dump of the chat MESSAGE-LIST DOM (container + a sample of
    // message rows with their attributes), so we can pin the real incoming-
    // message selectors for the anon light-meetings client and confirm the
    // end-command reader actually sees messages. Best-effort, never throws.
    private async dumpChatListDom(page: Page): Promise<void> {
        try {
            const info = await page.evaluate(() => {
                const attrs = (el: Element) => ({
                    tag: el.tagName.toLowerCase(),
                    id: (el as HTMLElement).id || undefined,
                    tid: el.getAttribute('data-tid') || undefined,
                    role: el.getAttribute('role') || undefined,
                    aria: el.getAttribute('aria-label') || undefined,
                    cls: ((el as HTMLElement).className?.toString?.() || '').slice(0, 80) || undefined,
                });
                const candidates = [
                    '#chat-pane-list',
                    '[data-tid="chat-pane-list"]',
                    '[data-tid="chat-pane-message-list"]',
                    '[data-tid="message-pane-list-runway"]',
                    '[data-tid="messages-list"]',
                    '[role="log"]',
                    '[data-tid="chat-pane"]',
                ];
                const containers = candidates
                    .map((s) => ({ s, el: document.querySelector(s) }))
                    .filter((c) => c.el)
                    .map((c) => ({ selector: c.s, ...attrs(c.el as Element) }));
                // Sample message-ish rows anywhere in the doc.
                const rows = Array.from(
                    document.querySelectorAll(
                        '[data-tid*="message" i], [role="heading"][aria-level="4"], .ui-chat__item, [id^="content-"]',
                    ),
                )
                    .slice(0, 8)
                    .map((el) => ({ ...attrs(el), text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 50) }));
                return { containers, rows };
            });
            console.log(`[chat-list-dom] containers=${JSON.stringify(info.containers)}`);
            console.log(`[chat-list-dom] sampleRows=${JSON.stringify(info.rows)}`);
        } catch (e) {
            console.warn('[chat-list-dom] dump failed (non-fatal):', e);
        }
    }

    // One-time diagnostic dump of the real in-meeting chat DOM. Teams' light-
    // meetings (anon) client uses different ids/classes than the full v2 client,
    // and our selector guesses kept missing the composer/send-button — so dump
    // the ground truth (every contenteditable + chat-region button with its
    // attributes) to the logs so we can pin exact selectors. Best-effort, never
    // throws. Logged once per send pass.
    private async dumpChatDom(page: Page, label: string): Promise<void> {
        try {
            const info = await page.evaluate(() => {
                const attrs = (el: Element) => ({
                    tag: el.tagName.toLowerCase(),
                    id: (el as HTMLElement).id || undefined,
                    tid: el.getAttribute('data-tid') || undefined,
                    role: el.getAttribute('role') || undefined,
                    aria: el.getAttribute('aria-label') || undefined,
                    ph: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || undefined,
                    cls: ((el as HTMLElement).className?.toString?.() || '').slice(0, 120) || undefined,
                    vis: (() => {
                        const r = (el as HTMLElement).getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    })(),
                });
                const editables = Array.from(document.querySelectorAll('[contenteditable="true"], [role="textbox"], textarea')).map(attrs);
                const sendBtns = Array.from(
                    document.querySelectorAll(
                        'button[aria-label*="send" i], button[data-tid*="send" i], button[name*="send" i], [data-tid="newMessageCommands"] button',
                    ),
                ).map(attrs);
                // Buttons inside any open chat/conversation region.
                const chatRegion =
                    document.querySelector('[data-tid="chat-pane"], #chat-pane-list, [aria-label*="chat" i][role="complementary"], [data-tid="messageBody"]') ||
                    document.body;
                const regionBtns = Array.from(chatRegion.querySelectorAll('button')).slice(0, 25).map(attrs);
                return { editables, sendBtns, regionBtns };
            });
            console.log(`[chat-dom ${label}] editables=${JSON.stringify(info.editables)}`);
            console.log(`[chat-dom ${label}] sendButtons=${JSON.stringify(info.sendBtns)}`);
            console.log(`[chat-dom ${label}] chatRegionButtons=${JSON.stringify(info.regionBtns)}`);
        } catch (e) {
            console.warn(`[chat-dom ${label}] dump failed (non-fatal):`, e);
        }
    }

    // Post chat messages. Resilient and NON-FATAL: the VP is already admitted
    // and transcribing by the time this runs, so a chat failure must never abort
    // the join — we log and return.
    //
    // The Teams composer is CKEditor 5. execCommand('insertText') silently
    // failed: it mutates the contenteditable's VISIBLE DOM but does NOT fire the
    // input events CKEditor's internal MODEL listens to — model stayed empty,
    // the visible-text check passed (fooling us), and submit sent nothing. Fix:
    // insert the message via a synthetic 'paste' ClipboardEvent (CKEditor's
    // model consumes paste in ONE shot — instant, vs. the slow per-keystroke
    // page.keyboard.type() that made a long intro crawl in visibly), falling
    // back to keystrokes if the paste doesn't land. Submit by clicking the real
    // Send button (data-tid="newMessageCommands-send", confirmed via DOM dump);
    // the anon light-meetings composer does not reliably submit on Enter.
    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        try {
            if (!(await this.chatInputVisible(page))) {
                await this.openChatPanel(page);
            }
            const found = await findElementWithFallback(
                page,
                Teams.CHAT_INPUT_SELECTORS,
                {
                    intent: 'Microsoft Teams in-meeting chat compose input area (contenteditable message box)',
                    platform: 'TEAMS',
                    step: 'teams.chat.input',
                    useScreenshot: true,
                },
                { maxRetries: 6, delayMs: 500 },
            );
            if (!found) {
                await this.dumpChatDom(page, 'no-input');
                console.log('Could not locate Teams chat input — skipping message post (non-fatal).');
                return;
            }
            console.log(`Teams chat input resolved via ${found.source} selector: ${found.selector}`);
            for (const message of messages) {
                // Focus + click the composer so the caret is inside it.
                try {
                    await found.element.evaluate((el) => {
                        const n = el as HTMLElement;
                        n.scrollIntoView({ block: 'center' });
                        n.focus();
                    });
                    await found.element.click({ timeout: 5000 }).catch(() => {
                        /* contenteditable focus above is enough; click is best-effort */
                    });
                    // Insert the whole message at once via a synthetic paste.
                    // CKEditor consumes the 'paste' ClipboardEvent and updates its
                    // model in one shot — instant, vs. ~1 key/CDP-roundtrip with
                    // keyboard.type() which made a long intro crawl in visibly.
                    // (execCommand('insertText') is NOT usable here — it skips the
                    // input pipeline CKEditor listens to; see the prior fix.)
                    const pasted = await found.element
                        .evaluate((el, text) => {
                            const node = el as HTMLElement;
                            node.focus();
                            try {
                                const dt = new DataTransfer();
                                dt.setData('text/plain', text as string);
                                const evt = new ClipboardEvent('paste', {
                                    clipboardData: dt,
                                    bubbles: true,
                                    cancelable: true,
                                });
                                node.dispatchEvent(evt);
                                return true;
                            } catch {
                                return false;
                            }
                        }, message)
                        .catch(() => false);
                    // Give the editor a tick to apply the paste, then check it
                    // landed; if not, fall back to real keystrokes (delay:0 is the
                    // fastest reliable keystroke path).
                    await new Promise((r) => setTimeout(r, 120));
                    const afterPaste = await found.element
                        .evaluate((el) => ((el as HTMLElement).innerText || el.textContent || '').trim())
                        .catch(() => '');
                    if (!pasted || !afterPaste.includes(message.slice(0, Math.min(20, message.length)))) {
                        console.log(`Paste insert did not land (pasted=${pasted}) — typing with keystrokes.`);
                        await page.keyboard.type(message, { delay: 0 });
                    }
                } catch (e) {
                    console.warn('Chat composer typing failed (non-fatal), skipping rest:', e);
                    return;
                }
                // Verify the text actually entered the composer before submitting.
                const typed = await found.element
                    .evaluate((el) => ((el as HTMLElement).innerText || el.textContent || '').trim().slice(0, 120))
                    .catch(() => '');
                const ok = typed.includes(message.slice(0, Math.min(20, message.length)));
                console.log(`Composer content after insert: "${typed}" (matches=${ok}).`);

                // Submit. Click the real Send button (primary), fall back to
                // Enter. Then verify the composer cleared (message left the box).
                const sent = await this.clickSendButton(page);
                if (!sent) {
                    console.log('Send button not found — falling back to Enter key.');
                    await page.keyboard.press('Enter');
                }
                await new Promise((resolve) => setTimeout(resolve, 500));
                const stillThere = await found.element
                    .evaluate((el, text) => ((el as HTMLElement).innerText || el.textContent || '').includes((text as string).slice(0, 20)), message)
                    .catch(() => false);
                if (stillThere) {
                    console.log('Composer still holds text after submit — retrying Enter once.');
                    await page.keyboard.press('Enter');
                    await new Promise((resolve) => setTimeout(resolve, 400));
                } else {
                    console.log('Chat message submitted (composer cleared).');
                }
                await new Promise((resolve) => setTimeout(resolve, 150));
            }
        } catch (e) {
            console.warn('sendMessages failed (non-fatal — VP remains in meeting):', e);
        }
    }

    // Click the chat "Send" button. The anon light-meetings client does NOT
    // reliably submit on Enter, so this is the primary submit path. Confirmed
    // selector from the in-meeting DOM dump: data-tid="newMessageCommands-send".
    // Returns whether a click was dispatched.
    private async clickSendButton(page: Page): Promise<boolean> {
        try {
            const found = await findElementWithFallback(
                page,
                [
                    'button[data-tid="newMessageCommands-send"]',
                    'button[name="send"]',
                    'button[aria-label*="send" i]',
                    'button[data-tid*="send" i]',
                ],
                {
                    intent: 'Microsoft Teams chat "Send" button that submits the typed message (paper-plane icon, label "Send").',
                    platform: 'TEAMS',
                    step: 'teams.chat.send',
                },
                { maxRetries: 4, delayMs: 300 },
            );
            if (!found) return false;
            await humanClick(page, found.element);
            return true;
        } catch {
            return false;
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
        // Forward in-page console output for the speaker/attendee/message
        // observers (they emit `DEBUG:`-prefixed logs that otherwise go nowhere)
        // so we can see in CloudWatch whether messageChange actually fires — key
        // for diagnosing the chat end-command reader on the anon client.
        page.on('console', (msg) => {
            const t = msg.text();
            if (t.startsWith('DEBUG:') || t.includes('messageChange') || t.includes('speakerChange')) {
                console.log(`Browser: ${t}`);
            }
        });
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

        // Admitted — we are in the meeting and about to start transcribing.
        // Mark JOINED FIRST, before any chat/intro work. Everything below
        // (opening the chat panel, posting the intro, switching to speaker view)
        // is best-effort cosmetics: the VP is already a successful participant,
        // so a failure in any of it must NEVER flip the join to FAILED. This is
        // exactly the bug that bit us — a non-visible chat element threw "failed
        // visible check" and aborted an otherwise-successful join.
        if (details.invite.virtualParticipantId) {
            const statusManager = createStatusManager(details.invite.virtualParticipantId);
            await statusManager.setJoined();
        }

        await substep('In the meeting — opening chat panel…');
        console.log("Opening chat panel.");
        await this.openChatPanel(page);

        await substep('In the meeting — posting introduction message…');
        console.log("Sending introduction messages.");
        await this.sendMessages(page, details.introMessages);
        await substep('In the meeting — listening for participants and transcript…');

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

        // All the in-meeting monitoring wiring below (attendee/speaker/message
        // observers) is set up AFTER we've marked JOINED. It's resilient
        // best-effort: a transient DOM/CDP error while installing any observer
        // must NOT abort an admitted VP (the crash latch in index.ts would flip
        // it to FAILED). Wrap the whole block; transcription start happens after
        // it regardless.
        try {
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
        const chatListInfo = await page.evaluate(() => {
            // The incoming-message DOM differs between the full v2 client and the
            // anon light-meetings client (the old hardcoded `#chat-pane-list` +
            // `div[dir="auto"][role="heading"][aria-level="4"]` only matched v2,
            // so the end command / chat commands were never read on anon joins).
            // Attach to the broadest chat container we can find and extract each
            // incoming message's text from several known node shapes. Observing a
            // bit broadly is fine — messageChange is idempotent via the seen-set.
            const CONTAINER_SELECTORS = [
                '#chat-pane-list',
                '[data-tid="chat-pane-list"]',
                '[data-tid="chat-pane-message-list"]',
                '[data-tid="message-pane-list-runway"]',
                '[data-tid="messages-list"]',
                '[role="log"]',
                '[data-tid="chat-pane"]',
            ];
            let container: Element | null = null;
            let matchedSelector = '';
            for (const s of CONTAINER_SELECTORS) {
                const el = document.querySelector(s);
                if (el) {
                    container = el;
                    matchedSelector = s;
                    break;
                }
            }
            // Fall back to body so we still catch messages even if no known
            // container id matches — the message-node extraction below is
            // specific enough to avoid false positives.
            const target = container || document.body;

            // Pull the human-readable message text out of an added chat node,
            // trying the known message-body shapes across both clients.
            const MESSAGE_SELECTORS = [
                'div[dir="auto"][role="heading"][aria-level="4"]', // v2 client
                '[data-tid="messageBodyContent"]',
                '[data-tid="chat-pane-message"] [dir="auto"]',
                'div[id^="content-"] [dir="auto"]',
                '.ui-chat__messagecontent',
                '[data-tid="messageContent"]',
            ];
            const extractText = (element: Element): string | null => {
                for (const sel of MESSAGE_SELECTORS) {
                    const node = element.matches?.(sel) ? element : element.querySelector(sel);
                    if (node) {
                        const t = (node.textContent || '').trim();
                        if (t) return t;
                    }
                }
                return null;
            };

            const seen = new Set<string>();
            const config = { childList: true, subtree: true };
            const callback = (mutationList: MutationRecord[]) => {
                for (const mutation of mutationList) {
                    for (const addedNode of mutation.addedNodes) {
                        if (addedNode.nodeType !== Node.ELEMENT_NODE) continue;
                        const text = extractText(addedNode as Element);
                        if (text && !seen.has(text)) {
                            seen.add(text);
                            (window as any).messageChange(text);
                        }
                    }
                }
            };
            const observer = new MutationObserver(callback);
            observer.observe(target, config);
            return { matchedSelector: matchedSelector || '(none — fell back to body)' };
        });
        console.log(`Teams chat message observer attached to: ${chatListInfo.matchedSelector}`);
        // One-shot diagnostic dump of the chat-list DOM so we can confirm the
        // real light-meetings container/message-node selectors from the logs.
        await this.dumpChatListDom(page);
        } catch (e) {
            // A monitoring-setup failure is non-fatal: the VP is admitted and
            // (below) transcribing. Speaker/attendee attribution may be degraded
            // but the meeting still works.
            console.warn('In-meeting monitoring setup failed (non-fatal — VP remains in meeting):', e);
        }

        // Start transcription if enabled (LMA behavior). This is the actual
        // point of the VP, so it runs regardless of any best-effort chat /
        // monitoring hiccups above.
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
