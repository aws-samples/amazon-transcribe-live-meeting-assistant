import { spawn } from 'child_process';
import { Page, Frame } from 'playwright-core';
import { details, matchesEndCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from './details.js';
import { transcriptionService } from './scribe.js';
import { createStatusManager } from "./status-manager.js";
import { voiceAssistant } from './voice-assistant.js';
import { simliAvatar } from './simli-avatar.js';
import { findElementWithFallback } from './ai-dom-resolver.js';
import { gotoMeetingPage, MEETING_HOST_PATTERNS } from './meeting-navigation.js';
import { startDialogWatchdog } from './dialog-watchdog.js';
import { humanClick } from './prejoin-actions.js';

/**
 * The pre-join display-name field, in both markups Webex serves.
 *
 * Classic web client: a plain `input[data-test="Name (required)"]`. Newer builds:
 * an `<mdc-input data-test="Name">` Momentum custom element whose real `<input>`
 * lives in an open shadow root, several levels below the host — so the selector
 * has to resolve the inner input, not the host, or `fill()` rejects the element
 * as not being an input.
 *
 * Playwright's CSS engine pierces open shadow roots on its own, which is why a
 * plain descendant selector works here. `>>>` does NOT: Playwright removed that
 * combinator and now parses it as `>>` plus a `:scope > input` part, which matches
 * only a direct child of the shadow root and so never reaches Momentum's nested
 * input.
 *
 * Declared once because it is needed in four places, and an invalid selector in
 * any one of them throws on sight rather than timing out — a comma list with one
 * bad alternative poisons the whole selector, even when the other alternative is
 * present on the page. See webex-selectors.test.ts.
 */
export const NAME_INPUT = 'input[data-test="Name (required)"], mdc-input[data-test="Name"] input';

/** Selectors the in-page chat observer needs, passed into `frame.evaluate`. */
export interface WebexChatSelectors {
    /** Chat message list. `current` is preferred over `legacy` when both match. */
    containers: { current: string; legacy: string };
    /** Sender label within one message row. */
    senders: string;
    /** Message body within one message row. */
    bodies: string;
    /** The row itself, resolved from whichever node the observer saw added. */
    rows: string;
}

/**
 * Reduce a Webex chat sender label to a display name.
 *
 * The legacy markup's label is not a bare name: it reads `from Alice to everyone:`,
 * which is why the own-message filter matches the `from LMA` prefix. Passing that
 * through unmodified would post "Thanks from Alice to everyone: — I'll head out
 * now." into the meeting and store the same text on the meeting record. zoom.ts
 * strips the equivalent prefix for the same reason; the Momentum markup supplies a
 * bare name already and passes through unchanged.
 */
export function normalizeWebexSender(raw: string | null | undefined): string | null {
    if (!raw) return null;
    // Collapse first, so the result does not depend on the caller having trimmed.
    const label = raw.replace(/\s+/g, ' ').trim();
    // The recipient clause is only stripped from a label that carries the legacy
    // "from " prefix — which is the form this file has always recognised, since the
    // own-message filter matches "from LMA". Anything else is taken as a display
    // name and left alone apart from a trailing colon, because a name is not
    // reliably separable from a recipient: "Van To Nguyen:" would otherwise reduce
    // to "Van", and silently shortening a participant's name is worse than leaving
    // a label slightly long.
    if (!/^from\s/i.test(label)) return label.replace(/:$/, '').trim() || null;
    const body = label.replace(/^from\s+/i, '').replace(/:$/, '').trim();
    // Greedy, so the LAST " to " is taken as the boundary: a sender whose own name
    // contains the word keeps it.
    const withRecipient = /^(.+)\sto\s\S/i.exec(body);
    if (withRecipient) return withRecipient[1].trim() || null;
    // A recipient clause with no sender part in front of it tells us nothing.
    if (/^to(\s|$)/i.test(body)) return null;
    return body || null;
}

/**
 * Is this chat message one of the VP's own?
 *
 * Runs on the node side, on the NORMALIZED sender, so one notion of "who sent this"
 * serves both this check and the goodbye. Doing it in the page against the raw label
 * let anything decorative through — a trailing colon, a leading '@', an appended
 * timestamp — and the VP would then ingest its own intro message, or toggle itself
 * on an operator-customised start or stop message containing START or PAUSE.
 *
 * Matched whole rather than as a substring: `scribeName` is the bare string "LMA",
 * and a substring test silences any participant whose display name contains it
 * (ALMA, SELMA, HOLMAN). "You" is matched only on its own or with a parenthesised
 * role after it, for the same reason — "You (Host)" is us, while "Youssef Ahmed" and
 * "You Jin Park" are participants.
 */
export function isOwnWebexSender(sender: string | null, identities: string[]): boolean {
    if (!sender) return false;
    // Drop a leading mention marker before comparing.
    const name = sender.replace(/^@+/, '').trim();
    if (matchesDecorated(name, 'You')) return true;
    return identities.some((own) => own && matchesDecorated(name, own));
}

/**
 * Is this message text one the VP itself sent?
 *
 * A backstop for the case where the chat markup gives no usable sender label, so the
 * name-based check cannot fire. Compares against the configured message lists rather
 * than guessing, so a deployment that customises them is covered too.
 */
export function isOwnWebexMessage(message: string): boolean {
    const text = message.trim();
    if (!text) return false;
    return [
        ...details.introMessages,
        ...details.startMessages,
        ...details.pauseMessages,
        ...details.exitMessages,
    ].some((own) => own && own.trim() === text);
}

/**
 * Does `name` consist of exactly `base`, allowing only the decorations Webex adds?
 *
 * A parenthesised role ("LMA (bob@example.com)", "You (Host)") and a trailing
 * timestamp are accepted; anything else is a different person. Deliberately NOT a
 * prefix match: `scribeName` is the bare string "LMA", so accepting any
 * space-delimited prefix silences a participant called "LMA Smith" — and with a
 * short LMA_IDENTITY it gets worse ("You" would silence "You Jin Park", "Bot" would
 * silence "Bot Smith").
 */
function matchesDecorated(name: string, base: string): boolean {
    const escaped = base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(
        `^${escaped}(\\s*\\([^)]*\\))?(\\s+\\d{1,2}:\\d{2}(\\s*[AP]M)?)?$`,
        'i',
    ).test(name);
}

/**
 * Chat selectors for both markups Webex serves — the Momentum (MDC) panel it uses
 * now, and the older CSS-module markup. Declared here rather than inline in the
 * observer so they are covered by webex-selectors.test.ts.
 */
export const WEBEX_CHAT_SELECTORS: WebexChatSelectors = {
    containers: {
        current: '#activity-list mdc-list',
        legacy: 'div[class^="style-chat-box"]',
    },
    senders: 'h3[class^="style-chat-label"], .sender-name',
    bodies: 'span[class^="style-chat-msg"], .activity-item-message',
    rows: '.activity-item, div[class^="style-chat-msg-container"]',
};

export default class Webex {
    private readonly iframe = '#unified-webclient-iframe';
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};
    // Set once the chat end-command has been handled, so the message observer
    // firing twice for the same message doesn't trigger a double exit/goodbye.
    private endHandled = false;

    constructor() {
        this.endRequested = new Promise<ExitInfo>((resolve) => {
            this.requestEnd = resolve;
        });
    }

    // Run an xdotool command against the Xvfb display, resolving with stdout.
    // Best-effort: resolves '' on any error so callers never throw.
    private xdotool(args: string[]): Promise<string> {
        return new Promise((resolve) => {
            try {
                const proc = spawn('xdotool', args, {
                    env: { ...process.env, DISPLAY: process.env.DISPLAY || ':99' },
                });
                let out = '';
                proc.stdout?.on('data', (d) => { out += d.toString(); });
                proc.on('error', () => resolve(''));
                proc.on('exit', () => resolve(out.trim()));
            } catch {
                resolve('');
            }
        });
    }

    // Dismiss the native external-protocol ("Open Webex.app?") chooser. This is
    // a Chromium browser-CHROME modal (a Views widget), NOT page DOM — so
    // Playwright's page.keyboard, page.on('dialog'), and CDP Input/Page dialog
    // APIs all silently no-op against it (they target the page renderer). The
    // only thing that reaches it is a real X11 input event, so we send a real
    // Escape (= the focused "Cancel" button) to the Chromium window via xdotool
    // on the Xvfb display. Tries `attempts` times because the chooser re-fires
    // (observed: needs dismissing twice). Best-effort; never throws.
    private async dismissNativeDialog(page: Page, attempts = 3): Promise<void> {
        for (let i = 0; i < attempts; i++) {
            try {
                // Focus the active Chromium window, then send Escape to it. Using
                // the active window avoids guessing the window id; key --window
                // delivers the event even if focus drifted.
                const winId = await this.xdotool(['getactivewindow']);
                if (winId) {
                    await this.xdotool(['key', '--window', winId, '--clearmodifiers', 'Escape']);
                } else {
                    // Fallback: search for the Chromium window by class.
                    const search = await this.xdotool(['search', '--onlyvisible', '--class', 'chrom']);
                    const wid = search.split('\n').filter(Boolean).pop();
                    if (wid) {
                        await this.xdotool(['windowactivate', '--sync', wid]);
                        await this.xdotool(['key', '--window', wid, '--clearmodifiers', 'Escape']);
                    } else {
                        // Last resort: type Escape to whatever has focus.
                        await this.xdotool(['key', '--clearmodifiers', 'Escape']);
                    }
                }
                console.log(`[webex] Sent X11 Escape (xdotool) to dismiss native dialog (attempt ${i + 1}/${attempts}, win=${winId || 'n/a'}).`);
            } catch (e) {
                console.log('[webex] dismissNativeDialog xdotool failed (non-fatal):', e);
            }
            await new Promise((r) => setTimeout(r, 800));
        }
    }

    private async sendMessages(
        frame: Frame,
        messages: string[],
        isEnterprise: boolean | null = false
    ): Promise<void> {
        // Cap the input lookup so a stale/missing chat editor selector can't hang
        // the caller forever (the goodbye-on-exit path is time-sensitive).
        const messageElement = await frame.waitForSelector(
            isEnterprise ? '#chat-panel > div > textarea' : '.ql-editor[contenteditable="true"]',
            { timeout: 8000 },
        );
        for (const message of messages) {
            await messageElement?.type(message);
            await messageElement?.press('Enter');
        }
        console.log('Sent messages:', messages);
    }

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        // Webex has no heavy credentialled sign-in phase, so bring the Simli
        // avatar up now (timing unchanged from before the deferral refactor).
        if (opts.prepareAvatar) await opts.prepareAvatar();
        // AI-driven dialog watchdog runs for the entire meeting lifecycle.
        // See dialog-watchdog.ts. Catches sign-in / pre-join / waiting-room /
        // in-meeting dialogs (consent, recording notice, captcha, SSO, etc.)
        // and either auto-dismisses (CONSENT-class) or escalates to
        // MANUAL_ACTION_REQUIRED so the user can clear it via VNC.
        startDialogWatchdog(page, { platform: 'WEBEX' });

        // The j.php launch link makes Webex auto-fire a native external-protocol
        // ("Open xdg-open?") chooser to launch the desktop app. That dialog is
        // browser chrome — it does NOT block clicking the page DOM behind it, so
        // the real join fix is the robust "Join from this browser" click below.
        // We still best-effort dismiss any dialog that surfaces so it can't grab
        // focus: a CDP Page.javascriptDialogOpening handler (some builds route
        // the chooser here) plus page.on('dialog') for JS-level dialogs. Note the
        // managed-policy / Preferences hints are unreliable against cloakbrowser's
        // patched Chromium binary, which is why we lean on the click, not the
        // dialog suppression, to actually join.
        try {
            const cdp = await page.context().newCDPSession(page);
            await cdp.send('Page.enable');
            // Auto-dismiss native JS dialogs (alert/confirm/beforeunload). Native
            // external-protocol choosers may also surface here on some builds.
            cdp.on('Page.javascriptDialogOpening', async () => {
                try {
                    await cdp.send('Page.handleJavaScriptDialog', { accept: false });
                    console.log('Auto-cancelled native dialog via CDP.');
                } catch { /* already handled */ }
            });
        } catch (err) {
            console.log('CDP dialog handler setup failed (non-fatal):', err);
        }
        page.on('dialog', async (dialog: any) => {
            try {
                console.log(`Auto-dismissing ${dialog.type()} dialog: ${dialog.message()}`);
                await dialog.dismiss();
            } catch {
                /* dialog already handled / page closed */
            }
        });

        // The meetingId is either a numeric Webex meeting number (entered into the
        // join-by-number form) or a full join URL such as a "j.php?MTID=..." launch
        // link. For a launch link the numeric ID/password cannot be derived from the
        // opaque MTID token, so we navigate straight to the URL and let Webex resolve
        // the meeting server-side (mirrors how Teams meetup-join URLs are handled).
        const meetingIdValue = (details.invite.meetingId || '').trim();
        const isJoinUrl = /^https?:\/\//i.test(meetingIdValue);

        if (isJoinUrl) {
            console.log('Navigating directly to Webex join URL.');
            await gotoMeetingPage(page, meetingIdValue, MEETING_HOST_PATTERNS.webex, 'webex-join');
            // Give the launch link time to redirect to the meeting join page.
            await new Promise(resolve => setTimeout(resolve, 3000));
        } else {
            console.log('Getting Webex meeting link.');
            await gotoMeetingPage(page, 'https://signin.webex.com/join', MEETING_HOST_PATTERNS.webex, 'webex-join');
            console.log('Entering meeting ID.');
            const meetingIdRes = await findElementWithFallback(
                page,
                ['#join-meeting-form'],
                {
                    intent: 'Webex landing page meeting-ID input field',
                    platform: 'WEBEX',
                    step: 'webex.join.meetingIdInput',
                },
                { maxRetries: 10, delayMs: 500 },
            );
            if (!meetingIdRes) {
                throw new Error('Webex meeting-ID input not found');
            }
            await meetingIdRes.element.type(meetingIdValue);
            await meetingIdRes.element.press('Enter');

            // Wait a moment for the page to stabilize after entering meeting ID
            await new Promise(resolve => setTimeout(resolve, 2000));
        }
        
        // The j.php launch link lands on a "Join your Webex meeting" chooser page
        // ("Download the Webex app" vs "Join from this browser", id
        // #broadcom-center-right — a DIV with role=button). Clicking it fires the
        // webex:// custom protocol, which raises Chromium's native "Open
        // Webex.app?" chooser. That dialog is browser CHROME (a Views widget),
        // NOT page DOM — it is modal and BLOCKS the join until dismissed, and the
        // first click is consumed by the app-launch attempt. The confirmed manual
        // flow is: click "Join from this browser" -> the native dialog pops ->
        // Cancel it -> click "Join from this browser" AGAIN -> the web client
        // ("Enter your information") finally loads. So we loop: click, dismiss the
        // native dialog via a real X11 Escape (dismissNativeDialog/xdotool — the
        // ONLY thing that reaches browser chrome), and repeat until the chooser
        // button is gone (= page advanced). (Older flows skip this chooser, so a
        // miss is non-fatal.)
        const CHOOSER_SELECTORS = [
            '#broadcom-center-right',
            '[role="button"]:contains("Join from this browser")',
            'button:contains("Join from this browser")',
            'a:contains("Join from this browser")',
        ];
        const chooserStillPresent = async (): Promise<boolean> => {
            try {
                return await page.evaluate(() => {
                    const el = document.querySelector('#broadcom-center-right');
                    if (!el) return false;
                    const r = (el as HTMLElement).getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
            } catch {
                return false;
            }
        };
        console.log('Checking for "Join from this browser" button...');
        try {
            // Up to 4 rounds of click + native-dialog dismiss. Each round: if the
            // chooser is still showing, click it and clear the resulting dialog.
            for (let round = 1; round <= 4; round++) {
                if (!(await chooserStillPresent())) {
                    console.log(`Chooser gone after ${round - 1} round(s) — page advanced past "Join from this browser".`);
                    break;
                }
                const res = await findElementWithFallback(
                    page,
                    CHOOSER_SELECTORS,
                    { intent: 'The "Join from this browser" button on the Webex app-download chooser', platform: 'WEBEX', step: 'webex.join.joinFromBrowser' },
                    { maxRetries: 4, delayMs: 500 },
                );
                if (!res) {
                    console.log(`Round ${round}: "Join from this browser" not found — assuming page advanced or auto-browser mode.`);
                    break;
                }
                console.log(`Round ${round}: clicking "Join from this browser" (${res.source}).`);
                await humanClick(page, res.element);
                // The native "Open Webex.app?" dialog appears shortly after the
                // click; give it a beat, then clear it with a real X11 Escape.
                await new Promise(resolve => setTimeout(resolve, 1500));
                await this.dismissNativeDialog(page, 2);
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
        } catch (error) {
            console.log('"Join from this browser" click loop failed (non-fatal), continuing:', error);
        }

        // Wait for the web client to load.
        await new Promise(resolve => setTimeout(resolve, 3000));
        console.log('Launching app.');
        // The web client renders in one of three places depending on the join
        // flow and Webex build:
        //   - 'default'    : classic web client inside #unified-webclient-iframe
        //   - 'enterprise' : enterprise web client inside iframe[name="thinIframe"]
        //   - 'mainframe'  : newer builds reached via a j.php launch link load the
        //                    client directly in the page (preloader.html?...runInOwnPage=true),
        //                    so there is NO iframe — the join UI lives in the main frame.
        // We race all three and take whichever appears first. The mainframe probe
        // watches for the pre-join UI itself (name/password/join controls) so we
        // don't mistake the app-download chooser page for a loaded client.
        const MAINFRAME_JOIN_UI = [
            NAME_INPUT,
            'input[aria-label="Meeting password"]',
            'mdc-button[data-test="join-button"]',
        ].join(', ');
        const frameElement = await Promise.any([
            page.waitForSelector(this.iframe, { timeout: 30000 }).then((el: any) => ({ source: 'default', el })).catch(() => null),
            page.waitForSelector('iframe[name="thinIframe"]', { timeout: 30000 }).then((el: any) => ({ source: 'enterprise', el })).catch(() => null),
            page.waitForSelector(MAINFRAME_JOIN_UI, { timeout: 30000 }).then(() => ({ source: 'mainframe', el: null })).catch(() => null),
        ]).catch(() => null);

        // For the iframe variants resolve the content frame; for the mainframe
        // variant the join UI is on the page itself.
        let frame = frameElement?.source === 'mainframe'
            ? page.mainFrame()
            : await frameElement?.el?.contentFrame();
        if (!frame) {
            // Known frame selectors didn't match — fall back to the main frame so
            // the flow can still proceed (best-effort) rather than hard-failing.
            console.log('Known Webex frame selectors did not match — falling back to main frame.');
            frame = page.mainFrame();
        }
        console.log(`Webex web client located in: ${frameElement?.source ?? 'mainframe-fallback'}`);
        await page.evaluate(() => {
            const checkAndClosePopup = () => {
                const dialog = document.querySelector('.el-dialog__wrapper');
                if (dialog && dialog.textContent?.includes('Problem joining from browser?')) {
                    const closeButton = dialog.querySelector('.el-dialog__close');
                    if (closeButton) {
                        console.log('Auto-closing "Problem joining from browser?" popup');
                        (closeButton as HTMLElement).click();
                        return true;
                    }
                }
                return false;
            };

            // Check immediately
            if (!checkAndClosePopup()) {
                const observer = new MutationObserver(() => {
                    if (checkAndClosePopup()) {
                        observer.disconnect();
                    }
                });
                observer.observe(document.body, { childList: true, subtree: true });
            }
        });

        const passwordCheckEl = await Promise.any([
            frame.waitForSelector('input[aria-label="Meeting password"]', { timeout: 30000 }).then((el: any) => ({ source: 'password', el })).catch(() => null),
            frame.waitForSelector(NAME_INPUT, { timeout: 30000 }).then((el: any) => ({ source: 'name', el })).catch(() => null),
            frame.waitForSelector('input[aria-labelledby="nameLabel"]', { timeout: 30000 }).then((el: any) => ({ source: 'enterprise-name', el })).catch(() => null)
        ]).catch(() => null);
    
        // Handle password page if detected
        if (passwordCheckEl && passwordCheckEl.source === 'password') {
            const passwordInput = passwordCheckEl.el;
            
            // Check if password is required and available
            if (details.invite.meetingPassword) {
                console.log('Auto-filling meeting password...');
                if (passwordInput) {
                    await passwordInput.type(details.invite.meetingPassword);
                }
            } else {
                console.log('ERROR: Meeting requires password but none was provided.');
                throw new Error('Meeting requires password but none was provided in invite details');
            }
            
            // Check for CAPTCHA
            const captchaImage = await frame.$('img[alt="Captcha image"]');
            if (captchaImage) {
                console.log('CAPTCHA detected! Triggering human-in-the-loop...');
                
                // Notify frontend that manual action is required
                if (details.invite.virtualParticipantId) {
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.setManualActionRequired(
                        'CAPTCHA',
                        'CAPTCHA detected on Webex password page. Please solve the CAPTCHA in the VNC viewer and click Next.',
                        120
                    );
                }
                
                // Wait for CAPTCHA to be solved (Next button to be enabled and clicked, or name input to appear)
                console.log('Waiting for CAPTCHA to be solved (up to 2 minutes)...');
                await Promise.race([
                    // Wait for name input to appear (successful CAPTCHA solve + Next click)
                    frame.waitForSelector(NAME_INPUT, {
                        timeout: 120000,
                        state: 'visible'
                    }),
                    // Or wait for the Next button to be clicked (we'll detect by it disappearing)
                    frame.waitForFunction(
                        () => {
                            const nextBtn = document.querySelector('mdc-button[type="submit"]');
                            return !nextBtn || nextBtn.getAttribute('disabled') === null;
                        },
                        undefined,
                        { timeout: 120000 }
                    )
                ]);
                
                console.log('CAPTCHA appears to be resolved, continuing...');
                await new Promise((resolve) => setTimeout(resolve, 2000));
                
                // Clear manual action notification after CAPTCHA is resolved
                if (details.invite.virtualParticipantId) {
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.clearManualAction();
                }
            } else {
                // No CAPTCHA, just password - click Next button
                console.log('No CAPTCHA detected, clicking Next button...');
                const nextButton = await frame.waitForSelector('mdc-button[type="submit"]', { timeout: 5000 });
                await nextButton?.click();
                await new Promise((resolve) => setTimeout(resolve, 2000));
            }
        }

        console.log('Entering name (and email on enterprise)');
        if (frameElement && frameElement.source === 'enterprise' && passwordCheckEl.source === 'enterprise-name') {
            // Check for guest form (name/email)
            const nameInput = await frame.$('input[aria-labelledby="nameLabel"]');
            console.log(`Guest form name input found: ${nameInput !== null}`);
            
            if (nameInput) {
                console.log('Enterprise Webex guest form detected, auto-filling name and email...');
                const emailInput = await frame.$('input[aria-labelledby="emailLabel"]');
                
                await nameInput.type(details.scribeIdentity);
                
                // Create a valid email from lmaUser
                let userEmail: string;
                if (details.lmaUser.includes('@')) {
                    // Already has @, use as-is
                    // userEmail = details.lmaUser; // enterprise emails might redirect to SSO login so use placeholder example
                    const sanitizedUser = details.lmaUser.split('@')[0].replace(/[^a-zA-Z0-9._-]/g, '-');
                    userEmail = `${sanitizedUser}@example.com`;
                } else {
                    // Sanitize username: keep only alphanumeric, dots, hyphens, underscores
                    const sanitizedUser = details.lmaUser.replace(/[^a-zA-Z0-9._-]/g, '-');
                    userEmail = `${sanitizedUser}@example.com`;
                }
                
                await emailInput?.type(userEmail);
                console.log(`Filled name: "${details.scribeIdentity}", email: "${userEmail}"`);
                
                // Wait for form validation to complete
                await new Promise(resolve => setTimeout(resolve, 2000));
                
                console.log('Clicking Next button...');
                const nextButton = await frame.$('#guest_next-btn');
                await nextButton?.click();
                
                // Wait for password page to load
                await new Promise(resolve => setTimeout(resolve, 5000));
                
                // Handle meeting password if present
                console.log('Checking for meeting password field...');
                const passwordInput = await frame.$('input[type="password"]');
                if (passwordInput && details.invite.meetingPassword) {
                    console.log('Password field detected, auto-filling meeting password...');
                    await passwordInput.type(details.invite.meetingPassword);
                    
                    console.log('Clicking Next button after password...');
                    const passwordNextButton = await frame.$('#password_validate_btn');
                    await passwordNextButton?.click();
                }
            }
        } else {
            const nameInputElement = (passwordCheckEl && passwordCheckEl.source === 'name')
                ? passwordCheckEl.el
                : await frame.waitForSelector(NAME_INPUT, { timeout: 30000 });
            // fill(), not type(): Webex persists the guest name in the VP's own
            // Chromium profile, so typing into a field that already holds "LMA"
            // appends and the meeting shows "LMALMA (user)". fill() selects the
            // existing text and replaces it, raising a native input event.
            await nameInputElement?.fill(details.scribeIdentity);
        }

        // Wait for the meeting interface (interstitial / pre-join) to load.
        await new Promise(resolve => setTimeout(resolve, 1500));

        const isEnterprise = !!(frameElement && frameElement.source === 'enterprise');

        // Click the first matching selector inside `frame`, trying each in order.
        // Best-effort: short per-selector timeout, never throws — a missed
        // pre-join toggle must not abort the whole join (the meeting can still be
        // entered with default mute/video state). Returns true if one matched.
        const tryClickInFrame = async (label: string, selectors: string[], perTimeout = 4000): Promise<boolean> => {
            for (const sel of selectors) {
                try {
                    const el = await frame.waitForSelector(sel, { timeout: perTimeout, state: 'visible' });
                    if (el) {
                        // Use evaluate-click for reliability inside the iframe (some
                        // Webex controls ignore positional clicks under automation).
                        await frame.evaluate((node: any) => node.click(), el);
                        console.log(`[webex] Clicked ${label} via "${sel}".`);
                        return true;
                    }
                } catch { /* try next selector */ }
            }
            console.log(`[webex] ${label} not found (non-fatal) — tried: ${selectors.join(' | ')}`);
            return false;
        };

        console.log('Handling cookie banner.');
        try {
            const rejectButton = await page.waitForSelector(
                isEnterprise ? '#cookie-banner-text > div.cookie-manage-option > div.cookie-banner-btnContainer > button:nth-child(1)' : '.cookie-banner-body .a32ueaoVYHwRrsRMl0ci mdc-button:first-child',
                { timeout: 3000 }
            );
            await rejectButton?.click();
            console.log('Successfully clicked Reject cookie button');
        } catch (error) {
            console.log('Cookie banner not found (non-fatal).');
        }

        // Mute (only if the voice assistant isn't driving audio). Non-fatal.
        if (!voiceAssistant.isEnabled()) {
            console.log('Clicking mute button.');
            await tryClickInFrame('mute button', isEnterprise
                ? ['#audioControlButton', 'button[data-doi*="MUTE"]', 'button[aria-label*="Mute" i]', 'button[title*="Mute" i]']
                : ['mdc-button[data-test="microphone-button"]', 'button[aria-label*="Mute" i]']);
        } else {
            console.log('Voice assistant enabled - skipping mute button for agent audio');
        }

        // Video: only turn it OFF if it's currently ON. On a headless VP with no
        // camera, this build renders the control as "Start video" with disabled=""
        // (video is already off). Clicking that tries to START video and pops a
        // blocking "No camera found" dialog — which previously wedged the join.
        // So: with Simli we leave video on (avatar camera); otherwise we click the
        // control ONLY when it's the STOP_VIDEO variant, and never the START_VIDEO
        // one. Then dismiss any "No camera found" popup as a safety net.
        if (simliAvatar.isConnected()) {
            console.log('Simli avatar active - keeping video ON for avatar camera.');
        } else if (isEnterprise) {
            // Inspect the video control in-frame and only click STOP_VIDEO.
            try {
                const clickedStop = await frame.evaluate(() => {
                    const stop = document.querySelector('button[data-doi*="STOP_VIDEO"]:not([disabled])') as HTMLElement | null;
                    if (stop) { stop.click(); return true; }
                    return false;
                });
                console.log(clickedStop
                    ? '[webex] Turned video off (clicked STOP_VIDEO).'
                    : '[webex] Video already off (no enabled STOP_VIDEO control) — leaving as-is.');
            } catch {
                console.log('[webex] Video state check failed (non-fatal) — leaving video as-is.');
            }
        } else {
            // Non-enterprise web client: best-effort camera-off, non-fatal.
            await tryClickInFrame('camera button', ['mdc-button[data-test="camera-button"]'], 3000);
        }

        // Safety net: dismiss a "No camera found" / "No video device" popup if one
        // is showing (its primary action is an "OK" button), so it can't block Join.
        try {
            const dismissed = await frame.evaluate(() => {
                const txt = (document.body.textContent || '').toLowerCase();
                if (txt.includes('no camera found') || txt.includes('camera device') || txt.includes('no video device')) {
                    const ok = Array.from(document.querySelectorAll('button'))
                        .find((b) => (b.textContent || '').trim().toLowerCase() === 'ok') as HTMLElement | undefined;
                    if (ok) { ok.click(); return true; }
                }
                return false;
            });
            if (dismissed) console.log('[webex] Dismissed "No camera found" popup (clicked OK).');
        } catch { /* non-fatal */ }

        // Join — this one DOES matter; if it misses, we can't enter. Try the known
        // selectors plus text/title fallbacks ("Join meeting"). Still non-throwing
        // so we fall through to the admission check below either way.
        console.log('Clicking join button.');
        const joined = await tryClickInFrame('join button', isEnterprise
            ? ['#interstitial_join_btn', 'button[data-doi*="JOIN"]', 'button[title*="Join" i]', 'button[aria-label*="Join meeting" i]']
            : ['mdc-button[data-test="join-button"]', 'button[title*="Join" i]'], 8000);
        if (!joined) {
            console.log('[webex] WARNING: Join button not matched by any selector on the interstitial.');
        }

        console.log("Opening chat panel.");
        try {
            const chatToggleButton = (frameElement && frameElement.source === 'enterprise') ? 'button[data-doi="CHAT:OPEN_CHAT_PANEL:MENU_CONTROL_BAR"]' : 'mdc-button[data-test="in-meeting-chat-toggle-button"]';
            await frame.waitForSelector(chatToggleButton, {
                timeout: details.waitingTimeout,
            });
            await frame.click(chatToggleButton);
            console.log("Chat panel button clicked successfully");
        } catch(error: any) {
            console.log("Chat panel button error:", error.message);
            console.log("Your scribe was not admitted into the meeting.");
            return { reason: 'unknown', trigger: 'pre-join:not-admitted' };
        }

        await new Promise(resolve => setTimeout(resolve, 1000));

        if (details.invite.virtualParticipantId) {
            const statusManager = createStatusManager(details.invite.virtualParticipantId);
            await statusManager.setJoined();
        }
        console.log('Successfully joined Webex meeting');

        console.log('Sending introduction messages.');
        await this.sendMessages(frame, details.introMessages, frameElement && frameElement.source === 'enterprise');

        // Set up speaker change monitoring
        await page.exposeFunction('speakerChange', async (speaker: string) => {
            await transcriptionService.speakerChange(speaker);
        });
        console.log("Listening for speaker changes.");
        await frame.evaluate(() => {
              const doc = document;

              // --- Helpers ------------------------------------------------------------
              // Accept both Document and Element for flexibility
              const NAME_SELECTORS = [
                '[data-test="participant-name"]',
                '.videoitem-full-name-content-TzQC4', // Enterprise Webex
                '[class*="full-name"]',
                'mdc-text[type="body-large-regular"]',
                'mdc-text',
                '[class*="name"]',
                '[data-test*="name"]',
              ];

              function getActiveSpeakerElement(root: Document | Element): Element | null {
                // Try enterprise Webex first (speaking indicator class)
                const enterpriseSpeaker = root.querySelector?.('.videoitem-in-speaking-3a-w-');
                if (enterpriseSpeaker) return enterpriseSpeaker;
                
                // Fall back to normal Webex (active speaker halo)
                return root.querySelector?.('.active-speaker-halo') ?? null;
              }

              function getParticipantItem(node: Element | null): Element | null {
                if (!node) return null;
                
                // For enterprise Webex, the node IS the video item container
                if (node.classList?.contains?.('videoitem-in-speaking-3a-w-')) {
                  return node;
                }
                
                // For normal Webex, find the closest participant container
                return (
                  node.closest?.(
                    'li,[role="listitem"],.participants-list-item,.participants-video-tile,.participants-video-panel-wrapper'
                  ) ?? null
                );
              }

              function extractNameFromItem(item: Element | null): string | null {
                  if (!item) return null;

                  for (const sel of NAME_SELECTORS) {
                      const el = item.querySelector?.(sel) as Element | null;
                      const text = el?.textContent?.trim();
                      if (text) return text;
                  }
                  const aria = item.getAttribute?.('aria-label')?.trim();
                  if (aria) return aria;
                  const text = item.textContent?.trim();
                  return text || null;
              }
              function getActiveSpeakerName(): string | null {
                const halo = getActiveSpeakerElement(doc);
                if (!halo) return null;
                const item = getParticipantItem(halo);
                return extractNameFromItem(item);
              }
              let lastAnnounced = "";
              function announceIfChanged() {
                const name = getActiveSpeakerName();
                if (name && name !== lastAnnounced) {
                  lastAnnounced = name;
                  console.log(`Speaker changed to: ${name}`);
                  (window as any).speakerChange?.(name);
                }
              }
              // Initial scan
              announceIfChanged();
              const observer = new MutationObserver((mutations) => {
                for (const m of mutations) {
                  if (m.type === 'childList') {
                    // Added/removed/moved halo or enterprise speaking indicator?
                    for (const n of [...m.addedNodes, ...m.removedNodes]) {
                      if (
                        n instanceof Element &&
                        (n.matches?.('.active-speaker-halo') ||
                         n.querySelector?.('.active-speaker-halo') ||
                         n.matches?.('.videoitem-in-speaking-3a-w-') ||
                         n.querySelector?.('.videoitem-in-speaking-3a-w-'))
                      ) {
                        announceIfChanged();
                        return;
                      }
                    }
                  } else if (m.type === 'attributes') {
                    const t = m.target as Element;
                    if (
                      t.matches?.('.active-speaker-halo') ||
                      t.classList?.contains?.('active-speaker-halo') ||
                      t.querySelector?.('.active-speaker-halo') ||
                      t.matches?.('.videoitem-in-speaking-3a-w-') ||
                      t.classList?.contains?.('videoitem-in-speaking-3a-w-') ||
                      t.querySelector?.('.videoitem-in-speaking-3a-w-')
                    ) {
                      announceIfChanged();
                      return;
                    }
                  }
                }
              });

              observer.observe(doc.body, {
                subtree: true,
                childList: true,
                attributes: true,
                attributeFilter: ['class', 'style', 'hidden', 'aria-hidden'],
              });

              const interval = setInterval(announceIfChanged, 500);

              (window as any).__webexActiveSpeakerStop = () => {
                observer.disconnect();
                clearInterval(interval);
              };
            });
        // Set up message monitoring with LMA features. The page passes the sender
        // and the body as separate arguments (as chime.ts does) rather than packing
        // them into one string: a body that happens to contain the delimiter would
        // otherwise be split into a bogus sender and a truncated message.
        await page.exposeFunction('messageChange', async (rawSender: string | null, message: string) => {
            const sender = normalizeWebexSender(rawSender);
            // Never act on, or transcribe, our own chat messages. The sender is
            // the primary test, but it cannot fire when no label resolves — a
            // continuation row, or the fallback where the row selector misses — so
            // our own outgoing text is recognised as well. That matters because the
            // start and stop messages are operator-settable and could otherwise
            // contain the literal START or PAUSE and toggle the VP.
            if (
                isOwnWebexSender(sender, [details.scribeIdentity, details.scribeName]) ||
                isOwnWebexMessage(message)
            ) {
                return;
            }
            if (matchesEndCommand(message)) {
                // Guard against the observer firing twice for the same message
                // (it can emit duplicate add events) — only act on the first.
                if (this.endHandled) return;
                this.endHandled = true;
                console.log(`LMA Virtual Participant has been asked to leave by ${sender || 'a participant'}: ${JSON.stringify(message)}`);
                details.start = false;
                // Post the goodbye BEFORE signalling exit — requestEnd triggers
                // teardown which closes the page, and a goodbye sent after that
                // fails with "Target page ... has been closed". We bound the send
                // (sendMessages has an 8s waitForSelector cap, and we race a 6s
                // ceiling on top) so a stale chat-input selector can't hang the
                // exit indefinitely (the original bug) — worst case we wait a few
                // seconds, then leave regardless.
                await Promise.race([
                    this.sendMessages(frame, exitMessagesFor(sender)),
                    new Promise((r) => setTimeout(r, 6000)),
                ]).catch((e) => console.log('Goodbye message send failed (non-fatal):', e));
                this.requestEnd({
                    reason: 'end-command',
                    trigger: 'chat',
                    requestedBy: sender,
                    matchedMessage: message,
                });
            } else if (
                details.start &&
                message.includes(details.pauseCommand)
            ) {
                details.start = false;
                console.log(details.pauseMessages[0]);
                await this.sendMessages(frame, details.pauseMessages);
            } else if (
                !details.start &&
                message.includes(details.startCommand)
            ) {
                details.start = true;
                console.log(details.startMessages[0]);
                await this.sendMessages(frame, details.startMessages);
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
        // The page's own console.log does NOT reach CloudWatch under CloakBrowser
        // (only warnings and errors do — see the note in audio-diagnostics.ts), and
        // webex.ts installs no console listener of its own, so anything the
        // observer needs to report has to come back as a return value and be
        // logged here on the node side.
        const chatObserverAttached = await frame.evaluate(
            ({ containers, senders, bodies, rows }: WebexChatSelectors) => {
                // Two chat markups are supported: the Momentum (MDC) panel Webex
                // serves now, and the older CSS-module markup some variants still
                // serve. The preference is explicit rather than a single comma
                // list, because a selector list resolves by DOCUMENT ORDER, not by
                // which generation we would rather have — a page carrying both
                // would otherwise bind to whichever happens to come first.
                const targetNode =
                    document.querySelector(containers.current) ??
                    document.querySelector(containers.legacy);
                // Returning false rather than logging: failing silently here is how
                // the previous breakage went unnoticed until a user reported that
                // chat commands did nothing.
                if (!targetNode) return false;

                // Bodies can be delivered more than once (a virtualized list
                // re-inserting a node, or two mutations touching the same row), and
                // details.messages has no idempotence of its own. Keyed on the BODY
                // element, not the row: chat UIs commonly group a run of messages
                // from one sender into a single row, and keying on the row would
                // drop every message in such a group after the first.
                //
                // Note the key is element identity, which survives a node being
                // MOVED but not REBUILT: an innerHTML re-render of the list produces
                // fresh elements and re-delivers, while a recycled element given new
                // text is not delivered again. Neither is how Lit or React normally
                // update, and the end-command path is idempotent via endHandled.
                const seen = new WeakSet<Element>();

                const callback = (mutationList: MutationRecord[]) => {
                    // Every added node in every mutation: a batch can carry two
                    // messages, and taking only the last one dropped the first —
                    // including, in the worst case, the "LMA leave" that preceded
                    // an ordinary message.
                    for (const mutation of mutationList) {
                        for (const node of Array.from(mutation.addedNodes)) {
                            // A text node means the body element was already there
                            // and has only now been filled, which is how a Lit
                            // render shows up; work from its parent in that case.
                            const el =
                                node instanceof Element ? node : node.parentElement;
                            if (!el) continue;
                            // Resolve the whole row, not just the node that was
                            // added: a Lit-rendered list inserts the row shell
                            // first and fills the sender and body in later
                            // mutations, so searching only inside the added node
                            // finds neither.
                            const row = el.closest(rows) ?? el;
                            // EVERY body under this node, not just the first. Chat
                            // UIs commonly group a run of messages from one sender
                            // into one row, and querySelector would return only that
                            // row's first message — dropping an "LMA leave" sent
                            // straight after the sender's previous line, which is
                            // the very defect this observer is being fixed for.
                            // `closest(bodies)` covers the case where the body is an
                            // ANCESTOR of the added node (a body filled by inserting
                            // a child element), which a downward scan misses.
                            // Walk out to the OUTERMOST enclosing body, bounded by
                            // the row: a mention or link chip inside a message also
                            // matches `bodies` (they share a class prefix), and taking
                            // the chip itself would deliver a fragment of a message
                            // that has already been delivered whole.
                            const outermostBody = (node: Element): Element => {
                                let outer = node;
                                for (let p = node.parentElement; p && p !== row; p = p.parentElement) {
                                    if (p.matches(bodies)) outer = p;
                                }
                                return outer;
                            };
                            const nearestBody = el.matches(bodies) ? el : el.closest(bodies);
                            const found = nearestBody
                                ? [outermostBody(nearestBody)]
                                : Array.from(row.querySelectorAll(bodies));
                            // Outermost matches only. `bodies` matches by class
                            // PREFIX, and the row selector shows several classes share
                            // it, so a mention or link span inside a message would
                            // otherwise be delivered a second time as a fragment —
                            // and a fragment can satisfy the end-command match where
                            // the whole sentence does not.
                            const bodyEls = found.filter(
                                (b) => !found.some((other) => other !== b && other.contains(b)),
                            );
                            for (const bodyEl of bodyEls) {
                                // Resolve the sender from THIS body's own row, not
                                // once for the added node. `closest` only walks up,
                                // so when the added node contains several rows — a
                                // bare text or comment node appended to the chat list
                                // makes the whole LIST the fallback scope, and a
                                // virtualized list can insert a wrapper of several
                                // rows — one label would otherwise be credited to
                                // every message under it. Since the VP's own start
                                // message is the first thing in the panel, that also
                                // meant the whole batch was discarded as "ours".
                                //
                                // When the body has no row of its own, NO sender is
                                // taken. Borrowing from the enclosing scope was still
                                // cross-message attribution, just narrower: a label
                                // belonging to an earlier attachment-only row, or to
                                // the VP's own not-yet-filled row shell, would be
                                // applied to this message — publicly naming the wrong
                                // person in the goodbye, or discarding the command as
                                // "ours". An unknown sender is delivered as null: the
                                // message still gets through and the goodbye falls
                                // back to its generic wording.
                                const scope = bodyEl.closest(rows);
                                // Ignore labels belonging to a NESTED row (a quoted or
                                // replied-to message): take the one at this row's own
                                // level, which is not necessarily the first in
                                // document order.
                                const label = Array.from(scope?.querySelectorAll(senders) ?? []).find(
                                    (candidate) => (candidate.closest(rows) ?? scope) === scope,
                                );
                                const sender = label?.textContent?.trim();
                                // Already delivered, or no text yet — a row shell
                                // whose content arrives in a later mutation, an
                                // attachment card, or a join/leave notice. Nothing
                                // to hand to the node side, which cannot take
                                // undefined. Deliberately NOT marked seen, so a
                                // staged render is read once it is complete.
                                if (seen.has(bodyEl)) continue;
                                const message = bodyEl.textContent?.trim();
                                if (!message) continue;
                                seen.add(bodyEl);
                                // Own-message filtering happens on the NODE side,
                                // after the label has been normalized — see
                                // isOwnWebexSender. Doing it here meant comparing a
                                // raw label, which any decoration defeated: a
                                // trailing colon, an '@', or an appended timestamp
                                // all let the VP's own messages through.
                                //
                                // Sender as a separate argument so a body containing
                                // the delimiter cannot be mistaken for one.
                                (window as any).messageChange(sender ?? null, message);
                            }
                        }
                    }
                };

                new MutationObserver(callback).observe(targetNode, { childList: true, subtree: true });
                return true;
            },
            WEBEX_CHAT_SELECTORS,
        );
        console.log(
            chatObserverAttached
                ? 'Webex chat observer attached.'
                : 'Webex chat container not found — in-meeting chat commands will not work.',
        );

        // Start transcription if enabled
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(frame, details.startMessages, frameElement && frameElement.source === 'enterprise');
            transcriptionService.startTranscription();
        }

        console.log('Waiting for meeting end.');
        let exitInfo: ExitInfo = { reason: 'unknown' };
        try {
            // Detect Webex's own meeting-end UI by watching the iframe text.
            // The detected substring distinguishes "meeting ended" (host) from
            // "you have left the meeting" / "disconnected" (page-closed-ish).
            const meetingEndDetected = new Promise<ExitInfo>(async (resolve) => {
                await page.exposeFunction('meetingEndDetected', (matched: string) => {
                    const text = matched.toLowerCase();
                    const reason: ExitInfo['reason'] =
                        text.includes('disconnected') || text.includes('left the meeting')
                            ? 'page-closed'
                            : 'host-ended';
                    resolve({ reason, trigger: `webex-text:${matched}` });
                });
                await frame.evaluate(() => {
                    const PHRASES = [
                        'meeting has ended',
                        'this meeting has ended',
                        'meeting ended',
                        'you have left the meeting',
                        'meeting disconnected',
                    ];
                    const matchPhrase = (text: string) => PHRASES.find((p) => text.includes(p)) || null;
                    const observer = new MutationObserver(() => {
                        const bodyText = document.body?.textContent?.toLowerCase() || '';
                        const hit = matchPhrase(bodyText);
                        if (hit) {
                            (window as any).meetingEndDetected(hit);
                            observer.disconnect();
                        }
                    });
                    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
                    const initial = matchPhrase((document.body?.textContent || '').toLowerCase());
                    if (initial) {
                        (window as any).meetingEndDetected(initial);
                        observer.disconnect();
                    }
                });
            });

            const meetingTimeout = new Promise<ExitInfo>((resolve) =>
                setTimeout(() => resolve({ reason: 'meeting-timeout', trigger: 'meetingTimeout' }), details.meetingTimeout),
            );

            exitInfo = await Promise.race([this.endRequested, meetingEndDetected, meetingTimeout]);
        } catch (error) {
            console.log('Meeting ended with error:', error);
            exitInfo = { reason: 'unknown', trigger: `error:${error instanceof Error ? error.message : String(error)}` };
        } finally {
            details.start = false;
        }
        console.log(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
        return exitInfo;
    }
}
