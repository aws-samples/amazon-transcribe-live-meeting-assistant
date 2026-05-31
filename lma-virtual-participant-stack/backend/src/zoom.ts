import { Page, ConsoleMessage, ElementHandle } from 'playwright-core';
import { details, matchesEndCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from './details.js';
import { transcriptionService } from './scribe.js';
import { voiceAssistant } from './voice-assistant.js';
import { simliAvatar } from './simli-avatar.js';
import {
    findElementWithFallback,
    isResolverEnabled,
    scrollIntoViewAndClick,
    classifyJoinState,
} from './ai-dom-resolver.js';
import { startDialogWatchdog } from './dialog-watchdog.js';
import { fetchZoomCredentials, loginToZoom, dismissPostLoginInterstitials } from './zoom-login.js';

// Zoom's audio/video toggles use SVG icons inside a clickable <button>.
// SVGs aren't reliable click targets, so we walk up to the nearest
// clickable ancestor before clicking.
async function clickClickableAncestor(element: ElementHandle<Element>): Promise<void> {
    await element.evaluate((el) => {
        const target = (el.closest('button, [role="button"], a') as HTMLElement | null) || (el as HTMLElement);
        target.scrollIntoView({ block: 'center', inline: 'center' });
        target.click();
    });
}

// Meaningful UX-style clicks (Join, Sign In, Skip-this-step). force:true skips
// Playwright's "covered by another element" actionability check (Zoom often
// floats a transient overlay over the Join/chat buttons); the old Puppeteer
// click didn't do that check, so this preserves prior behavior.
async function humanClick(
    page: Page,
    target: string | ElementHandle<Element>,
): Promise<void> {
    if (typeof target === 'string') {
        await page.click(target, { force: true });
    } else {
        await target.click({ force: true });
    }
}

// Type into a (possibly overlay-covered) input. Playwright's ElementHandle
// .type() runs an actionability/pointer check that fails when Zoom floats a
// transient overlay over the prejoin form. Focus the element directly in the
// DOM (no actionability check), then type via the keyboard with a small
// per-keystroke delay so input lands reliably in Zoom's SPA fields.
async function humanType(
    page: Page,
    element: ElementHandle<Element>,
    text: string,
): Promise<void> {
    await element.evaluate((el) => (el as HTMLElement).focus());
    await page.keyboard.type(text, { delay: 50 + Math.floor(Math.random() * 70) });
}

// Run a page.evaluate with a wall-clock timeout. A CDP evaluate can hang
// indefinitely (page mid-navigation, execution context destroyed, renderer
// under load) — observed freezing the whole join sequence at the post-join
// popup-handler install, so the VP joined the room but never opened chat /
// posted the intro / went ACTIVE. On timeout this resolves to `fallback`
// (default undefined) and logs, so the join sequence keeps moving rather than
// blocking forever. Use for non-critical setup evaluates in the join path.
async function safeEvaluate<T>(
    page: Page,
    label: string,
    fn: () => T | Promise<T>,
    ms = 8000,
    fallback?: T,
): Promise<T | undefined> {
    let to: NodeJS.Timeout | undefined;
    try {
        return await Promise.race<T>([
            page.evaluate(fn),
            new Promise<T>((_, reject) => {
                to = setTimeout(() => reject(new Error(`evaluate "${label}" timed out after ${ms}ms`)), ms);
            }),
        ]);
    } catch (e: any) {
        console.warn(`[zoom] safeEvaluate(${label}) failed/timed out: ${e?.message || e}`);
        return fallback;
    } finally {
        if (to) clearTimeout(to);
    }
}

export default class Zoom {
    // Resolved by the message handler ("LMA END"), the attendee-change handler
    // ("only attendee left"), or anywhere else that wants to end the meeting
    // explicitly. The wait-for-meeting-end Promise.race below listens for it.
    // Using an in-process signal — instead of the legacy `page.goto('about:blank')`
    // hack — keeps the Chromium tab on the meeting URL until the cleanup chain
    // in index.ts is ready to close the browser, which avoids orphaning the
    // exposed-function callback that initiated the end and lets profile-store
    // run to completion.
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};

    constructor() {
        this.endRequested = new Promise<ExitInfo>((resolve) => {
            this.requestEnd = resolve;
        });
    }

    /**
     * Backwards-compat wrapper used by the SVG audio/video toggle paths that
     * need to know WHICH selector matched (mute vs unmute, on vs off).
     * Tries each primary selector in order with retry, then falls back to AI
     * resolution if all primaries miss.
     */
    private async waitForButtonWithRetry(
        page: Page,
        selectors: string[],
        step: string,
        intent: string,
        maxRetries: number = 10,
        delayMs: number = 500,
    ): Promise<{ element: ElementHandle<Element>; selector: string } | null> {
        const result = await findElementWithFallback(
            page,
            selectors,
            { intent, platform: 'ZOOM', step },
            { maxRetries, delayMs },
        );
        if (!result) {
            console.log(`Failed to resolve ${step} after retries (primaries: ${selectors.join(', ')})`);
            return null;
        }
        console.log(`Resolved ${step} via ${result.source}: ${result.selector}`);
        return { element: result.element, selector: result.selector };
    }

    // The AI-driven dialog watchdog logic is shared across all platform
    // handlers and lives in dialog-watchdog.ts. This thin wrapper preserves
    // the per-instance call signature and lets us also start the watchdog
    // before the join completes (so verification / consent dialogs that
    // block the prejoin/waiting-room are caught and escalated to MANUAL).
    private startUnknownDialogWatchdog(page: Page): void {
        startDialogWatchdog(page, { platform: 'ZOOM' });
    }

    /**
     * Wait for the Zoom prejoin form to settle before we fill it. After a
     * manual sign-in (e.g. the user solved a reCAPTCHA in VNC), Zoom bounces
     * the page through several redirects (app.zoom.us/signin → /wc/<id>/join)
     * and re-renders the prejoin form more than once. Filling the passcode
     * into a transient form that then re-mounts silently loses the input and
     * the VP sits unadmitted until timeout. Poll until a prejoin control
     * (passcode, name, or Join button) is visible AND the URL has been stable
     * briefly. Returns true if a prejoin control appeared, false on timeout.
     */
    private async waitForPrejoinReady(page: Page, timeoutMs = 30000): Promise<boolean> {
        const deadline = Date.now() + timeoutMs;
        let lastUrl = '';
        let stableSince = Date.now();
        while (Date.now() < deadline) {
            let url = lastUrl;
            try {
                url = page.url();
            } catch {
                await new Promise((r) => setTimeout(r, 300));
                continue;
            }
            if (url !== lastUrl) {
                lastUrl = url;
                stableSince = Date.now();
            }
            // A prejoin control present + URL stable for ~1.2s = settled.
            const hasControl = await page
                .evaluate(() => {
                    const sels = [
                        '#input-for-pwd',
                        '#input-for-name',
                        'button.preview-join-button',
                        'button.zm-btn.preview-join-button',
                    ];
                    for (const s of sels) {
                        const el = document.querySelector(s) as HTMLElement | null;
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) return true;
                        }
                    }
                    // Already in-meeting? Any in-meeting chrome counts as
                    // "ready" (no prejoin to wait for).
                    return !!document.querySelector(
                        '.video-avatar__avatar, [class*="footer-button"], footer.footer, [class*="meeting-client"]',
                    );
                })
                .catch(() => false);
            if (hasControl && Date.now() - stableSince > 1200) {
                console.log(`[zoom] prejoin settled at ${url}`);
                return true;
            }
            await new Promise((r) => setTimeout(r, 300));
        }
        console.warn(`[zoom] prejoin did not settle within ${timeoutMs}ms (at ${(() => { try { return page.url(); } catch { return '?'; } })()}); proceeding anyway`);
        return false;
    }

    /**
     * Robust "are we inside the meeting?" check. Zoom's web client UI changes
     * its class names across versions, so relying on a single selector (the
     * old `.video-avatar__avatar`) is fragile — when Zoom Workplace stopped
     * rendering that exact class, the VP joined but the admission poll never
     * saw it and sat at "Waiting." forever. Check a broad set of in-meeting
     * signals (footer toolbar, leave/audio/video controls, participants/chat
     * buttons, the speaker/gallery video containers) AND that prejoin controls
     * are gone. Any positive in-meeting signal counts.
     */
    private async isInMeeting(page: Page): Promise<boolean> {
        return page
            .evaluate(() => {
                // Still on prejoin? Then definitely not in-meeting.
                const onPrejoin =
                    document.querySelector('#input-for-pwd') ||
                    document.querySelector('button.preview-join-button, button.zm-btn.preview-join-button');
                if (onPrejoin) return false;

                const visible = (sel: string): boolean => {
                    const el = document.querySelector(sel) as HTMLElement | null;
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const inMeetingSignals = [
                    '.video-avatar__avatar',                 // legacy avatar tile
                    'footer.footer',                          // meeting footer bar
                    '[class*="footer-button"]',               // any footer control
                    '#foot-bar',
                    'button[aria-label*="audio" i]',          // mute/unmute
                    'button[aria-label*="mute" i]',
                    'button[aria-label*="video" i]',          // start/stop video
                    'button[aria-label*="participant" i]',    // participants panel
                    'button[aria-label*="chat" i]',           // chat panel
                    'button[aria-label*="leave" i]',          // leave button
                    'button[aria-label*="End" i]',
                    '[class*="meeting-client"]',
                    '[class*="speaker-view"]',
                    '[class*="gallery-video"]',
                    '[class*="sharee-container"]',
                ];
                return inMeetingSignals.some(visible);
            })
            .catch(() => false);
    }

    /** True once we've left the prejoin screen (join/passcode controls gone,
     *  or any in-meeting signal present). */
    private async hasLeftPrejoin(page: Page): Promise<boolean> {
        return page
            .evaluate(() => {
                const join = document.querySelector('button.preview-join-button, button.zm-btn.preview-join-button');
                const pwd = document.querySelector('#input-for-pwd');
                if (!join && !pwd) return true;
                // Or in-meeting chrome appeared even while a control lingers.
                return !!document.querySelector('[class*="footer-button"], footer.footer, [class*="meeting-client"]');
            })
            .catch(() => false);
    }

    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        const found = await findElementWithFallback(
            page,
            ['p[data-placeholder="Type message here ..."]'],
            {
                intent: 'Zoom chat panel message input (contenteditable)',
                platform: 'ZOOM',
                step: 'zoom.chat.input',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!found) {
            console.log('Could not locate chat message input — aborting sendMessages');
            return;
        }
        for (const message of messages) {
            await new Promise(resolve => setTimeout(resolve, 10));
            await humanType(page, found.element, message);
            await page.keyboard.press('Enter');
        }
    }

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        const prepareAvatar = opts.prepareAvatar ?? (async () => {});

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

        page.on('crash', () => {
            // Renderer crash (usually OOM). If this happens after we've joined
            // and are sitting in the wait-for-meeting-end race, requestEnd
            // unblocks it cleanly; before join, index.ts's crash latch wins the
            // initialize() race. Either way we never hang on a dead page.
            console.error('Browser Error: page crashed (renderer crash) — ending meeting');
            this.requestEnd({ reason: 'page-closed', trigger: 'renderer-crash' });
        });

        // Push a human-readable sub-step into the JOINING status so the long
        // join span (sign-in → navigate → prejoin → admission → chat) doesn't
        // look frozen in the UI. Best-effort, no-op without a vpId.
        const substep = async (message: string): Promise<void> => {
            if (!details.invite.virtualParticipantId) return;
            try {
                const { createStatusManager } = await import('./status-manager.js');
                await createStatusManager(details.invite.virtualParticipantId).setJoiningSubstep(message);
            } catch {
                /* best-effort progress */
            }
        };

        // Optional: sign in to Zoom first using user-provided credentials.
        // When the user has stored Zoom credentials in LMA Settings, the
        // launching state machine plumbs the secret name into this env var.
        // A signed-in session joins far more reliably (the "We detected
        // you may be a bot. ... sign in to join the meeting" guest block
        // becomes rare) and lets the VP join meetings that disallow guests.
        const zoomCredentialsSecretName = (process.env.ZOOM_CREDENTIALS_SECRET_NAME || '').trim();
        let signedInToZoom = false;
        if (zoomCredentialsSecretName) {
            // Phase: SIGNING_IN. Kept distinct from JOINING (below) in the logs
            // because it's the CPU-heavy phase (Bedrock + Zoom SPA hydration) and
            // the one that historically stalls; the Simli avatar is deliberately
            // NOT initialized yet so it doesn't contend for CPU here.
            console.log('[phase] SIGNING_IN — authenticating to Zoom before join');
            console.log(`Zoom credentials provided (secret: ${zoomCredentialsSecretName}); signing in before join.`);
            await substep('Signing in to Zoom…');
            try {
                const creds = await fetchZoomCredentials(zoomCredentialsSecretName);
                const loginResult = await loginToZoom(page, creds);
                if (loginResult.outcome === 'success') {
                    console.log('[zoom] Sign-in succeeded — profile will persist at meeting end');
                    signedInToZoom = true;
                } else if (loginResult.outcome === 'invalid-credentials') {
                    console.error('[zoom] Sign-in failed: invalid credentials');
                    throw new Error('Zoom login failed: invalid credentials');
                } else {
                    // manual-required: user must finish sign-in (CAPTCHA, 2FA,
                    // SSO, or — when the resolver flat-out couldn't find the
                    // form — sign in by hand). Escalate to the UI so we don't
                    // silently degrade to guest join (which joins far less
                    // reliably, and the user explicitly opted into
                    // credentialled login).
                    console.warn(`[zoom] Sign-in needs manual action (reason=${loginResult.manualReason || 'generic'}): ${loginResult.detail || ''} — escalating to user via VNC`);

                    // Build a clean, user-facing message by category. Never
                    // surface internal selector/loop detail (e.g. "action
                    // continue on #js_btn_login did not advance").
                    const manualMessage = (() => {
                        switch (loginResult.manualReason) {
                            case 'captcha':
                                return 'Zoom needs you to sign in manually. Open the LMA viewer and sign in to Zoom again (there is usually no puzzle — it just needs a human-driven sign-in). The participant will join automatically once you\'re signed in. If it keeps asking, open zoom.us/signin in your own browser and sign OUT first — being signed in to the same Zoom account in multiple places at once can cause this.';
                            case 'otp-2fa':
                                return 'Zoom is asking for a one-time / 2FA verification code. Open the LMA viewer and enter the code to finish signing in — the participant will join automatically afterward.';
                            case 'sso':
                                return 'Zoom is redirecting to your single sign-on (SSO) provider. Open the LMA viewer and complete the SSO sign-in — the participant will join automatically afterward.';
                            default:
                                return 'Zoom needs you to finish signing in manually. Open the LMA viewer and complete the Zoom sign-in — the participant will join automatically afterward.';
                        }
                    })();

                    // For a CAPTCHA, reload the sign-in page so the human gets
                    // a clean, fully-rendered form to work with rather than the
                    // half-interacted state the automation left behind.
                    if (loginResult.manualReason === 'captcha') {
                        try {
                            await page.goto('https://zoom.us/signin', { waitUntil: 'domcontentloaded' });
                            console.log('[zoom] Reloaded sign-in page for clean manual CAPTCHA sign-in.');
                        } catch (e) {
                            console.warn('[zoom] Could not reload sign-in page for manual login (non-fatal).');
                        }
                    }

                    if (details.invite.virtualParticipantId) {
                        const { createStatusManager } = await import('./status-manager.js');
                        const statusManager = createStatusManager(details.invite.virtualParticipantId);
                        await statusManager.setManualActionRequired(
                            'LOGIN',
                            manualMessage,
                            300,
                        );
                        // Give the user up to 5 minutes to finish the sign-in
                        // by hand. Strict success criterion: a real Zoom auth
                        // cookie ("zm_aid", "_zm_ssid", etc.) must be set.
                        // Cookies are HttpOnly so we can't see them via
                        // `document.cookie` in-page — node-side
                        // `page.cookies()` (CDP `Network.getCookies`) does
                        // return them. Poll every 2s for up to 5 minutes.
                        const SIGN_IN_TIMEOUT_MS = 300_000;
                        const POLL_MS = 2000;
                        // Only post-auth cookies — `cred` and `_zm_mtk_guid`
                        // are unconditionally set by /signin even pre-login,
                        // so they trigger false-positive auth detection.
                        const wantedCookies = new Set(['zm_aid', '_zm_ssid', 'zm_haid']);
                        const deadline = Date.now() + SIGN_IN_TIMEOUT_MS;
                        let signInOK = false;
                        while (Date.now() < deadline) {
                            try {
                                const url = page.url();
                                const onSignin = url.includes('/signin') || url.includes('/sso');
                                if (!onSignin) {
                                    const cookies = await page.context().cookies(['https://zoom.us', 'https://app.zoom.us']);
                                    if (cookies.some((c) => wantedCookies.has(c.name) && c.value)) {
                                        signInOK = true;
                                        break;
                                    }
                                }
                            } catch (pollErr: any) {
                                // page.cookies / page.url can throw if the page
                                // is mid-navigation; that's fine, just keep polling.
                                if (page.isClosed()) break;
                            }
                            await new Promise((r) => setTimeout(r, POLL_MS));
                        }
                        if (signInOK) {
                            signedInToZoom = true;
                            await statusManager.clearManualAction();
                            console.log('[zoom] User completed sign-in via VNC — profile will persist at meeting end');
                        } else {
                            // Don't auto-clear the manual-action banner — let
                            // the user see what happened. Mark as FAILED with
                            // a clear errorMessage so the UI / MCP poller
                            // surfaces the reason instead of silently joining
                            // as guest (which joins far less reliably).
                            console.warn('[zoom] User did not complete sign-in within 5 min — failing rather than joining as guest');
                            await statusManager.setFailed(
                                'Zoom sign-in not completed in time. Open the LMA viewer next time the VP starts and complete the sign-in there. Alternatively, sign in to Zoom on your laptop with this account at least once before launching LMA so the trusted-device cookie is established.',
                            );
                        }
                        if (!signInOK) {
                            // Hard-stop: don't navigate to the meeting URL at
                            // all — a guest join here would be unreliable.
                            throw new Error('Zoom sign-in not completed; aborting join');
                        }
                    }
                }
            } catch (err: any) {
                if (err?.message?.includes('Zoom login failed: invalid credentials')) {
                    throw err;
                }
                if (err?.message?.includes('Zoom sign-in not completed')) {
                    // Don't fall back to guest — keep the FAILED status set
                    // above and abort. Throwing here makes the outer
                    // initialize() catch update the VP status to FAILED with
                    // the underlying reason already on it.
                    throw err;
                }
                // Any other unexpected error during sign-in (e.g.
                // 'Execution context was destroyed' from a navigation racing
                // a page.evaluate) used to fall back to guest, which joins
                // far less reliably. The user explicitly opted into
                // stored-credential login, so guest-fallback violates their
                // intent. Mark FAILED and abort instead.
                console.error('[zoom] Sign-in attempt threw unexpectedly — failing rather than joining as guest:', err);
                if (details.invite.virtualParticipantId) {
                    const { createStatusManager } = await import('./status-manager.js');
                    const statusManager = createStatusManager(details.invite.virtualParticipantId);
                    await statusManager.setFailed(
                        `Zoom sign-in failed unexpectedly: ${err?.message || String(err)}. ` +
                            'Try again, or untick "Use my stored Zoom account" to join as a guest.',
                    );
                }
                throw new Error(`Zoom sign-in error: ${err?.message || String(err)}`);
            }
        }

        // Phase: JOINING. Sign-in (the CPU-heavy phase) is done. NOW bring up
        // the Simli avatar — its background render + ~20fps JPEG encode load
        // (~1800/2048 CPU units) is acceptable here and the avatar must be
        // ready before the prejoin camera toggle below captures the camera.
        console.log('[phase] JOINING — navigating to the meeting and joining');
        await substep('Entering the meeting room…');
        await prepareAvatar();

        console.log('Getting Zoom meeting link.');
        await page.goto(`https://zoom.us/wc/${details.invite.meetingId}/join`);

        // After arriving at the meeting URL, Zoom may redirect to a
        // post-login binding/upsell page (passkey, phone, SMS, "stay
        // signed in?"). Dismiss any "Skip for now" link so the user
        // doesn't have to do it manually. No-op when there's nothing
        // to dismiss; loop handles chained interstitials.
        if (signedInToZoom) {
            try {
                await dismissPostLoginInterstitials(page);
            } catch (err) {
                console.warn('[zoom] post-meeting-URL interstitial dismissal threw (non-fatal):', err);
            }
        }

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

                    // Notify frontend that manual action is required.
                    const { createStatusManager } = await import('./status-manager.js');
                    const sm = details.invite.virtualParticipantId
                        ? createStatusManager(details.invite.virtualParticipantId)
                        : null;
                    if (sm) {
                        await sm.setManualActionRequired(
                            'LOGIN',
                            'Enterprise Zoom authentication required. Please sign in using the VNC viewer.',
                            120,
                        );
                    }

                    // Wait up to 2 min for the human to complete enterprise
                    // login (avatar tile appears). ALWAYS clear the manual
                    // action afterwards — on success AND on timeout — so the
                    // UI never stays stuck on the "auth required" banner. On
                    // timeout, fail cleanly as never-joined instead of
                    // silently continuing into a prejoin flow that can't work.
                    let enterpriseLoggedIn = false;
                    const enterpriseDeadline = Date.now() + 120000;
                    while (Date.now() < enterpriseDeadline) {
                        if (page.isClosed()) break;
                        if (await this.isInMeeting(page)) {
                            enterpriseLoggedIn = true;
                            break;
                        }
                        await new Promise((resolve) => setTimeout(resolve, 1500));
                    }
                    if (enterpriseLoggedIn) {
                        await new Promise((resolve) => setTimeout(resolve, 5000));
                    } else {
                        console.warn('[zoom] Enterprise login not completed within 2 min.');
                    }
                    if (sm) {
                        await sm.clearManualAction().catch(() => {});
                    }
                    if (!enterpriseLoggedIn) {
                        return {
                            reason: 'never-joined',
                            trigger: 'enterprise-login-timeout',
                        };
                    }
                }
            }
        } catch (error) {
            // If selector times out, continue normally (no auth required)
        }

        // if they logged in with enterprise they won't need to put in name password etc so skip
        if (enterpriseLogin === false) {
            // Wait for the prejoin form to settle. Critical after a manual
            // sign-in (reCAPTCHA/2FA solved in VNC): Zoom redirects through
            // app.zoom.us/signin → /wc/<id>/join and re-renders the form, so
            // filling it too early loses the input. See waitForPrejoinReady.
            await substep('Loading the meeting join screen…');
            await this.waitForPrejoinReady(page);

            // A non-empty meetingPassword doesn't guarantee Zoom renders
            // #input-for-pwd — Personal Meeting Rooms and passcode-disabled
            // meetings skip it. Probe (primary-only; the selector is
            // stable) and proceed if absent rather than failing the join.
            // Type, verify the value landed, and re-type once if not (the
            // form can still re-mount under us right after settling).
            if (details.invite.meetingPassword) {
                const findPwd = async (): Promise<ElementHandle<Element> | null> => {
                    for (let attempt = 1; attempt <= 6; attempt++) {
                        const h = await page.$('#input-for-pwd');
                        if (h) {
                            const box = await h.boundingBox().catch(() => null);
                            if (box && box.width > 0 && box.height > 0) return h;
                        }
                        await new Promise((r) => setTimeout(r, 500));
                    }
                    return null;
                };
                let passwordHandle = await findPwd();
                if (passwordHandle) {
                    const pwd = details.invite.meetingPassword;
                    let entered = false;
                    for (let attempt = 1; attempt <= 2 && !entered; attempt++) {
                        console.log(`Typing meeting password (attempt ${attempt}).`);
                        await passwordHandle.evaluate((el: Element) => {
                            const i = el as HTMLInputElement;
                            i.focus();
                            i.value = '';
                        }).catch(() => {});
                        await humanType(page, passwordHandle, pwd);
                        const got = await passwordHandle
                            .evaluate((el: Element) => (el as HTMLInputElement).value || '')
                            .catch(() => '');
                        if (got.length === pwd.length) {
                            entered = true;
                        } else {
                            console.warn(
                                `Passcode value mismatch (expected ${pwd.length} chars, got ${got.length}) — re-locating and retrying.`,
                            );
                            const fresh = await findPwd();
                            if (!fresh) break;
                            passwordHandle = fresh;
                        }
                    }
                    if (!entered) {
                        console.warn('Could not reliably enter meeting passcode after retries — continuing; Join may reject it.');
                    }
                } else {
                    console.log('Meeting does not require a password — skipping password entry.');
                }
            }

            await substep('Setting up audio and video…');
            console.log('Checking audio button state with retry...');
            // Wait for audio button to appear (handles loading state)
            const audioResult = await this.waitForButtonWithRetry(
                page,
                ['svg.SvgAudioMute', 'svg.SvgAudioUnmute'],
                'zoom.join.audioToggle',
                'Zoom join-screen microphone toggle button (mute/unmute icon)',
            );
            
            if (audioResult && !voiceAssistant.isEnabled()) {
                if (audioResult.selector === 'svg.SvgAudioMute') {
                    console.log('Audio is unmuted, clicking to mute it.');
                    await clickClickableAncestor(audioResult.element);
                } else {
                    console.log('Audio is already muted, skipping click.');
                }
            } else if (voiceAssistant.isEnabled()) {
                console.log('Voice assistant enabled - keeping microphone unmuted for agent audio');
                if (audioResult && audioResult.selector === 'svg.SvgAudioUnmute') {
                    console.log('Audio is muted, clicking to unmute it for voice assistant.');
                    await clickClickableAncestor(audioResult.element);
                }
            } else {
                console.log('Warning: Could not find audio button in either state after retries.');
            }

            console.log('Checking video button state with retry...');
            // Wait for video button to appear (handles loading state)
            const videoResult = await this.waitForButtonWithRetry(
                page,
                ['svg.SvgVideoOn', 'svg.SvgVideoOff'],
                'zoom.join.videoToggle',
                'Zoom join-screen camera toggle button (video on/off icon)',
            );
            
            if (videoResult && simliAvatar.isConnected()) {
                // Simli avatar active - ensure video is ON, then force Zoom to
                // (re-)acquire the camera so our getUserMedia override actually
                // fires. The avatar reaches Zoom only when Zoom calls
                // getUserMedia({video}); if the prejoin already shows video as
                // "on", Zoom may never call it and the camera stays blank. A
                // deliberate off→on toggle guarantees a fresh getUserMedia.
                if (videoResult.selector === 'svg.SvgVideoOff') {
                    // Currently off → single click turns it on (fresh getUserMedia).
                    console.log('Simli avatar active - clicking to turn video ON for avatar camera.');
                    await clickClickableAncestor(videoResult.element);
                } else {
                    // Currently on → toggle off then on to force a fresh
                    // getUserMedia call that the override can intercept.
                    console.log('Simli avatar active - video already on; toggling off→on to force camera (re)acquire.');
                    await clickClickableAncestor(videoResult.element);
                    await new Promise((r) => setTimeout(r, 800));
                    const reVideo = await this.waitForButtonWithRetry(
                        page,
                        ['svg.SvgVideoOn', 'svg.SvgVideoOff'],
                        'zoom.join.videoToggle',
                        'Zoom join-screen camera toggle button (video on/off icon)',
                        6,
                        500,
                    );
                    if (reVideo && reVideo.selector === 'svg.SvgVideoOff') {
                        await clickClickableAncestor(reVideo.element);
                    } else if (reVideo) {
                        // Already back on somehow — leave it on.
                        console.log('Simli avatar active - video already back on after toggle.');
                    }
                }
                // Eagerly connect the relay consumer + canvas in the meeting
                // frames so the avatar feed is populated the moment Zoom pulls.
                await simliAvatar.connectStreamToMeetingPage(page).catch(() => null);
            } else if (videoResult) {
                if (videoResult.selector === 'svg.SvgVideoOn') {
                    console.log('Video is on, clicking to turn it off.');
                    await clickClickableAncestor(videoResult.element);
                } else {
                    console.log('Video is already off, skipping click.');
                }
            } else {
                console.log('Warning: Could not find video button in either state after retries.');
            }

            // Always overwrite the prejoin display name with the scribe
            // identity (a signed-in account otherwise pre-fills its own
            // name). Clear before typing — typing alone appends, yielding
            // e.g. "Bob Strahan-LMALMA (user@…)".
            const nameResult = await findElementWithFallback(
                page,
                ['#input-for-name'],
                {
                    intent: 'Zoom join-screen display-name input field',
                    platform: 'ZOOM',
                    step: 'zoom.join.name',
                },
                { maxRetries: 6, delayMs: 500 },
            );
            if (nameResult) {
                await nameResult.element.evaluate((el: Element) => {
                    const i = el as HTMLInputElement;
                    i.focus();
                    i.value = '';
                });
                console.log(`Setting display name to scribe identity ("${details.scribeIdentity}").`);
                await humanType(page, nameResult.element, details.scribeIdentity);
                const got = await nameResult.element.evaluate(
                    (el: Element) => (el as HTMLInputElement).value || '',
                );
                if (got !== details.scribeIdentity) {
                    console.warn(
                        `Display-name value mismatch (expected "${details.scribeIdentity}", got "${got}") — clearing and re-typing.`,
                    );
                    await nameResult.element.evaluate((el: Element) => {
                        const i = el as HTMLInputElement;
                        i.focus();
                        i.value = '';
                    });
                    await humanType(page, nameResult.element, details.scribeIdentity);
                }
            } else if (!signedInToZoom) {
                console.log('LMA Virtual Participant was unable to join the meeting.');
                throw new Error('Meeting not found or invalid meeting ID');
            } else {
                console.log('No prejoin name field on this page — skipping.');
            }

            // Submit the prejoin form, then VERIFY we actually left it. A
            // single click often silently no-ops when the passcode didn't
            // register (e.g. it was typed into a form that re-mounted), and
            // the VP then sits on the prejoin screen until waitingTimeout. So
            // we click, check whether we advanced, and if not re-enter the
            // passcode and re-click once before giving up to the admission
            // poll (which the dialog-watchdog also covers).
            const clickJoin = async (): Promise<void> => {
                const joinButtonResult = await findElementWithFallback(
                    page,
                    [
                        'button.zm-btn.preview-join-button',
                        'button.preview-join-button',
                        'button[type="submit"].zm-btn',
                    ],
                    {
                        intent: 'Zoom prejoin "Join" button that submits the meeting prejoin form',
                        platform: 'ZOOM',
                        step: 'zoom.join.submit',
                    },
                    { maxRetries: 10, delayMs: 500 },
                );
                if (joinButtonResult) {
                    console.log('Clicking Join button to enter the meeting.');
                    await humanClick(page, joinButtonResult.element);
                } else if (nameResult) {
                    console.log('Could not locate Join button — pressing Enter on name field as fallback.');
                    await nameResult.element.press('Enter').catch(() => {});
                } else {
                    console.log('Could not locate Join button or name field — pressing Enter on page.');
                    await page.keyboard.press('Enter').catch(() => {});
                }
            };

            await clickJoin();

            // Did the click advance us past the prejoin form? Poll briefly.
            const waitLeftPrejoin = async (ms: number): Promise<boolean> => {
                const until = Date.now() + ms;
                while (Date.now() < until) {
                    if (await this.hasLeftPrejoin(page)) return true;
                    await new Promise((r) => setTimeout(r, 500));
                }
                return false;
            };

            if (!(await waitLeftPrejoin(6000))) {
                console.warn('Join did not advance the prejoin screen — re-entering passcode and retrying once.');
                await substep('Re-attempting to join the meeting…');
                // Re-enter passcode if the field is still there (it being
                // present is itself the signal Join didn't take).
                if (details.invite.meetingPassword) {
                    const pwd2 = await page.$('#input-for-pwd');
                    if (pwd2) {
                        await pwd2.evaluate((el: Element) => {
                            const i = el as HTMLInputElement;
                            i.focus();
                            i.value = '';
                        }).catch(() => {});
                        await humanType(page, pwd2, details.invite.meetingPassword);
                    }
                }
                await clickJoin();
                if (await waitLeftPrejoin(6000)) {
                    console.log('Join succeeded on retry — left the prejoin screen.');
                } else {
                    console.warn('Still on prejoin after retry — handing off to admission poll / dialog-watchdog.');
                }
            } else {
                console.log('Left the prejoin screen — proceeding to admission wait.');
            }
        }

        // Start the AI-driven unknown-dialog watchdog BEFORE we begin the
        // waiting-room poll. Previously we only started this watchdog after
        // the meeting was joined (line ~679 below), which meant Zoom's
        // bot-detection dialog ("We detected you may be a bot") on the
        // prejoin page would sit unrecognized for the full waitingTimeout
        // (5 min) and the VP would exit silently. The watchdog asks Claude
        // (vision + DOM) to classify any visible modal — CONSENT auto-
        // clicks, CAPTCHA/BLOCKED/LOGIN_REQUIRED escalates to
        // MANUAL_ACTION_REQUIRED so the user can clear it via VNC.
        if (isResolverEnabled()) {
            this.startUnknownDialogWatchdog(page);
        }

        console.log('Waiting.');
        await substep('Waiting to be admitted to the meeting…');
        // Poll for the avatar selector instead of a single waitForSelector
        // call so we can dynamically extend the timeout when the watchdog
        // has escalated to MANUAL_ACTION_REQUIRED. Otherwise the 5-min
        // waitingTimeout fires while the human is still trying to clear
        // the verification / sign-in challenge in VNC, and the
        // VP exits before they finish.
        const POLL_INTERVAL_MS = 1500;
        const startWait = Date.now();
        const baseDeadline = startWait + details.waitingTimeout;
        // When MANUAL_ACTION is active, give the user up to 5 extra
        // minutes from the moment they cleared the original deadline.
        const MANUAL_ACTION_GRACE_MS = 5 * 60 * 1000;
        let admitted = false;
        let lastProgressBump = Date.now();
        // AI second-opinion cadence. The CSS heuristic (isInMeeting) is fast and
        // runs every poll; Claude's join-state classifier is expensive, so we
        // only consult it periodically while the heuristic keeps saying "not in
        // yet". This is the safety net for the failure mode where Zoom renamed
        // its in-meeting classes and the heuristic never fired even though we
        // were admitted (VP sat at "Waiting." forever). It also lets us end the
        // wait early on an error/removed/blocked screen instead of timing out.
        let lastAiCheck = Date.now();
        const AI_CHECK_INTERVAL_MS = 30_000;
        const sm = details.invite.virtualParticipantId
            ? (await import('./status-manager.js')).createStatusManager(
                  details.invite.virtualParticipantId,
              )
            : null;
        while (true) {
            if (page.isClosed()) break;
            try {
                if (await this.isInMeeting(page)) {
                    admitted = true;
                    break;
                }
            } catch {
                /* ignore — page may be navigating */
            }
            // Heuristic still says "not in". Ask Claude what screen we're really
            // on every AI_CHECK_INTERVAL_MS so a class-rename can't strand us.
            if (isResolverEnabled() && Date.now() - lastAiCheck > AI_CHECK_INTERVAL_MS) {
                lastAiCheck = Date.now();
                try {
                    const verdict = await classifyJoinState(page, { platform: 'ZOOM' });
                    if (verdict) {
                        console.log(`[zoom] AI join-state check: ${verdict.state} — ${verdict.reason}`);
                        if (verdict.state === 'in-meeting') {
                            console.log('[zoom] AI confirms we are in the meeting (CSS heuristic missed it) — proceeding.');
                            admitted = true;
                            break;
                        }
                        if (verdict.state === 'error') {
                            console.warn(`[zoom] AI detected an error/blocked screen during admission wait: ${verdict.reason}`);
                            return { reason: 'never-joined', trigger: 'pre-join:ai-error-screen' };
                        }
                    }
                } catch (e) {
                    console.warn('[zoom] AI join-state check failed (non-fatal):', e);
                }
            }
            // Keep the UI alive during the (potentially minutes-long) wait so
            // it never looks frozen — refresh the JOINING detail + updatedAt
            // every ~20s with elapsed seconds. Skip if MANUAL_ACTION is
            // active (that has its own banner we don't want to overwrite).
            if (Date.now() - lastProgressBump > 20000) {
                lastProgressBump = Date.now();
                const secs = Math.round((Date.now() - startWait) / 1000);
                const current = sm ? await sm.getCurrentStatus().catch(() => null) : null;
                if (current !== 'MANUAL_ACTION_REQUIRED') {
                    await substep(`Waiting to be admitted to the meeting… (${secs}s — host may need to admit the participant)`);
                }
            }
            // Check current status — if MANUAL_ACTION_REQUIRED, extend deadline.
            let extended = false;
            if (sm && Date.now() > baseDeadline) {
                try {
                    const current = await sm.getCurrentStatus();
                    if (current === 'MANUAL_ACTION_REQUIRED') {
                        // Grant extra grace; re-evaluate every poll until
                        // the watchdog clears the manual action OR the user
                        // hits the manualActionTimeoutSeconds (handled by
                        // the watchdog itself when the dialog disappears).
                        if (Date.now() < baseDeadline + MANUAL_ACTION_GRACE_MS) {
                            extended = true;
                        }
                    }
                } catch {
                    /* couldn't read status; fall through */
                }
            }
            if (Date.now() > baseDeadline && !extended) {
                console.log('LMA Virtual Participant was not admitted into the meeting.');
                return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
            }
            await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        }
        if (!admitted) {
            console.log('LMA Virtual Participant was not admitted into the meeting.');
            return { reason: 'never-joined', trigger: 'pre-join:not-admitted' };
        }

        // Dismiss any Zoom popups (recording consent, language interpretation, etc.)
        // Wrapped in safeEvaluate: this install observed hanging post-join
        // (CDP evaluate never resolving), which froze the whole join sequence
        // — the VP was in the room but never opened chat / posted intro / went
        // ACTIVE. A timeout lets the sequence proceed even if the install
        // stalls (the popup auto-dismiss is best-effort anyway).
        console.log('Setting up Zoom popup auto-dismiss handler.');
        await safeEvaluate(page, 'popup-dismiss-install', () => {
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

        // Brief settle for an immediately-present popup. The auto-dismiss
        // handler above is a self-running MutationObserver + poller, so we
        // don't need to block long here — keep this short so the intro
        // message posts ASAP after admission rather than a couple seconds
        // later. (The dominant historical delay was CPU contention from the
        // avatar during join, now removed by deferring Simli init.)
        await new Promise(resolve => setTimeout(resolve, 500));
        console.log('Popup handler active, proceeding to open chat.');

        // (Note: the unknown-dialog watchdog is already running — started
        // before the waiting-room poll above so it can catch verification /
        // blocking dialogs that appear on the prejoin page. It keeps running for
        // the rest of the meeting to catch consent/recording-notice/etc.
        // dialogs that may appear after the join.)

        console.log('Opening chat panel.');
        const chatButtonResult = await findElementWithFallback(
            page,
            ['button[aria-label="open the chat panel"]'],
            {
                intent: 'Zoom toolbar button that opens the in-meeting chat panel',
                platform: 'ZOOM',
                step: 'zoom.join.chatButton',
            },
            { maxRetries: 10, delayMs: 500 },
        );
        if (!chatButtonResult) {
            console.log('Could not locate chat panel button — continuing without chat');
        } else {
            await humanClick(page, chatButtonResult.element);
        }

        await substep('In the meeting — posting introduction…');
        console.log('Sending introduction messages.');
        await this.sendMessages(page, details.introMessages);

        // Set up attendee change monitoring
        await page.exposeFunction('attendeeChange', async (number: number) => {
            if (number <= 1) {
                console.log('LMA Virtual Participant got lonely and left.');
                details.start = false;
                this.requestEnd({ reason: 'alone-in-meeting', trigger: 'attendees-left' });
            }
        });

        // Resolve attendee-counter selector node-side first so we can fall back to AI if Zoom changes the class.
        let attendeeCounterSelector: string | null = '.footer-button__number-counter';
        if (!(await page.$(attendeeCounterSelector))) {
            const found = await findElementWithFallback(
                page,
                [],
                {
                    intent: 'Zoom toolbar element showing the current attendee count number',
                    platform: 'ZOOM',
                    step: 'zoom.monitor.attendeeCounter',
                },
                { maxRetries: 1, delayMs: 100 },
            );
            attendeeCounterSelector = found ? found.selector : null;
        }
        if (attendeeCounterSelector) {
            console.log(`Listening for attendee changes via selector: ${attendeeCounterSelector}`);
            await page.evaluate((selector: string) => {
                const targetNode = document.querySelector(selector);
                const config = { characterData: true, subtree: true };
                const callback = (mutationList: MutationRecord[]) => {
                    const number = parseInt(
                        mutationList[mutationList.length - 1].target.textContent || '0'
                    );
                    (window as any).attendeeChange(number);
                    if ((window as any).attendeeCountReport) {
                        (window as any).attendeeCountReport(number);
                    }
                };
                const observer = new MutationObserver(callback);
                if (targetNode) {
                    observer.observe(targetNode, config);
                    // Report initial value so the speaker-watchdog has a baseline.
                    const initial = parseInt(targetNode.textContent || '1');
                    if ((window as any).attendeeCountReport) {
                        (window as any).attendeeCountReport(initial);
                    }
                }
            }, attendeeCounterSelector);
        } else {
            console.log('No attendee counter selector available — skipping attendee monitoring');
        }

        // Participants-panel watchdog: the MutationObserver above only fires
        // when the count *number* changes. If Zoom kicks the VP out (host
        // removes, "your account signed in elsewhere", network drop showing
        // an error screen), the counter element disappears entirely and no
        // mutation event fires. Poll the panel every 30s as a backup —
        // this is also a more reliable "lonely VP" signal than the original
        // observer-based one, which sometimes misses the transition.
        // Decision rules:
        //   - counter element absent for >=2 consecutive polls (~60s) →
        //     VP no longer in the meeting → end.
        //   - counter present but count <= 1 for >=2 consecutive polls →
        //     VP is alone → end (matches existing behaviour).
        //   - counter present and count > 1 → reset both counters.
        // 60s grace absorbs transient React re-renders that briefly remove
        // the counter while the participants panel updates. Audio silence is
        // explicitly NOT a signal (long silent doc reviews are real).
        if (attendeeCounterSelector) {
            const counterSelector = attendeeCounterSelector;
            let consecutiveMissingCounter = 0;
            let consecutiveLonely = 0;
            const POLL_MS = 30_000;
            const POLLS_BEFORE_END = 2;
            const watchdogTimer = setInterval(async () => {
                if (page.isClosed()) {
                    clearInterval(watchdogTimer);
                    return;
                }
                let result: { state: string; count?: number };
                try {
                    result = await page.evaluate((sel: string) => {
                        const counter = document.querySelector(sel);
                        if (!counter) return { state: 'COUNTER_GONE' };
                        const n = parseInt(counter.textContent || '0', 10);
                        return { state: 'OK', count: Number.isFinite(n) ? n : 0 };
                    }, counterSelector);
                } catch {
                    // page.evaluate threw — page closed, navigated away, or CDP error.
                    result = { state: 'PAGE_DEAD' };
                }
                if (result.state === 'COUNTER_GONE' || result.state === 'PAGE_DEAD') {
                    consecutiveMissingCounter += 1;
                    consecutiveLonely = 0;
                    if (consecutiveMissingCounter >= POLLS_BEFORE_END) {
                        console.log(
                            `[participants-watchdog] counter absent for ${consecutiveMissingCounter} polls (state=${result.state}) — VP appears to have been removed from the meeting`,
                        );
                        clearInterval(watchdogTimer);
                        details.start = false;
                        this.requestEnd({
                            reason: 'removed-from-meeting',
                            trigger: `participants-ui-${result.state.toLowerCase()}`,
                        });
                    }
                } else if ((result.count ?? 0) <= 1) {
                    consecutiveLonely += 1;
                    consecutiveMissingCounter = 0;
                    if (consecutiveLonely >= POLLS_BEFORE_END) {
                        console.log(
                            `[participants-watchdog] only ${result.count} attendee(s) for ${consecutiveLonely} polls — leaving meeting`,
                        );
                        clearInterval(watchdogTimer);
                        details.start = false;
                        this.requestEnd({
                            reason: 'alone-in-meeting',
                            trigger: 'participants-watchdog',
                        });
                    }
                } else {
                    consecutiveMissingCounter = 0;
                    consecutiveLonely = 0;
                }
            }, POLL_MS);
        }

        // Set up speaker change monitoring
        let lastSpeakerEventAt = Date.now();
        let lastReportedAttendeeCount = 1;
        await page.exposeFunction('speakerChange', async (speaker: string) => {
            lastSpeakerEventAt = Date.now();
            await transcriptionService.speakerChange(speaker);
        });
        // Watch attendee count separately so the safety net can wait until
        // there's actually >1 person before flagging a stuck observer.
        await page.exposeFunction('attendeeCountReport', async (count: number) => {
            lastReportedAttendeeCount = count;
        });

        console.log('Listening for speaker changes.');
        await page.evaluate((vpIdentity: string) => {
            let observer: MutationObserver | null = null;
            let lastSpeaker: string | null = null;
            let micActivityTimeout: any = null;
            let isVPSpeaking = false;
            const MIC_ACTIVITY_THRESHOLD = 5; // px
            const MIC_SILENCE_DELAY = 2000; // ms - increased to 2 seconds to handle pauses in speech

            // Mutable selector list — node-side watchdog can inject a new
            // selector when Zoom changes the active-speaker DOM.
            (window as any).__lmaSpeakerSelectors = [
                // Normal mode - main view (prioritized: shows active speaker)
                '.single-main-container__video-frame .video-avatar__avatar-footer span',
                // Screen sharing mode - suspension window (small video)
                '.single-suspension-container__video-frame .video-avatar__avatar-footer span',
                // Fallback - any avatar footer
                '.video-avatar__avatar-footer span[role="none"]'
            ];
            (window as any).__lmaSetSpeakerSelectors = (extra: string[]) => {
                const cur = (window as any).__lmaSpeakerSelectors as string[];
                for (const s of extra) {
                    if (s && !cur.includes(s)) cur.unshift(s);
                }
            };

            // Function to get current speaker from any view
            function getCurrentSpeaker(): string | null {
                const selectors: string[] = (window as any).__lmaSpeakerSelectors;
                
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

        // Node-side liveness watchdog: if no speaker change has been seen for
        // 60s while the meeting reports >1 attendee, ask Claude to find the
        // active-speaker name selector and inject it into the in-page list.
        // Cached in DDB so subsequent meetings get the new selector instantly.
        if (isResolverEnabled()) {
            const SPEAKER_STALL_MS = 60_000;
            const watchdog = setInterval(async () => {
                try {
                    if (page.isClosed()) {
                        clearInterval(watchdog);
                        return;
                    }
                    const idleMs = Date.now() - lastSpeakerEventAt;
                    if (idleMs < SPEAKER_STALL_MS) return;
                    if (lastReportedAttendeeCount <= 1) return;
                    console.log(
                        `[speaker-watchdog] no speaker events in ${Math.floor(idleMs / 1000)}s with ${lastReportedAttendeeCount} attendees — asking AI for a fresh selector`,
                    );
                    const found = await findElementWithFallback(
                        page,
                        [],
                        {
                            intent: 'Element on the Zoom meeting screen that shows the name label of the currently active/spotlighted speaker (the participant whose video tile is highlighted as speaking)',
                            platform: 'ZOOM',
                            step: 'zoom.monitor.activeSpeaker',
                        },
                        { maxRetries: 1, delayMs: 100 },
                    );
                    if (found) {
                        await page.evaluate((sel: string) => {
                            const fn = (window as any).__lmaSetSpeakerSelectors;
                            if (fn) fn([sel]);
                        }, found.selector);
                        // Reset the timer so we don't spam the resolver if the new selector also stalls.
                        lastSpeakerEventAt = Date.now();
                    }
                } catch (err) {
                    console.warn('[speaker-watchdog] error:', err);
                }
            }, 30_000);
        }

        // Set up message monitoring with LMA features
        await page.exposeFunction('messageChange', async (message: string) => {
            // Zoom's chat aria-label has two observed shapes:
            //   colon form:     "<sender> to <recipient>: <text>"
            //   timestamp form: "<sender> to <recipient>, HH:MM AM, <text>"
            // (Zoom shipped the timestamp form during 2026; older clients
            // still produce the colon form.) Match either, but always anchor
            // on the literal "<sender> to <recipient>" prefix so we don't
            // mistake punctuation inside the message for the boundary.
            const senderMatch = message.match(
                /^([^,:]+?)\s+to\s+[^,:]+?(?:\s*:\s*|,\s*\d{1,2}:\d{2}(?:\s*[AP]M)?\s*,\s*)/i,
            );
            const sender = senderMatch?.[1]?.trim() || null;
            const body = senderMatch ? message.slice(senderMatch[0].length) : message;
            if (matchesEndCommand(body)) {
                console.log(`LMA Virtual Participant has been asked to leave by ${sender || 'a participant'}: ${JSON.stringify(body)}`);
                await this.sendMessages(page, exitMessagesFor(sender));
                details.start = false;
                this.requestEnd({
                    reason: 'end-command',
                    trigger: 'chat',
                    requestedBy: sender,
                    matchedMessage: body,
                });
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

        // Resolve chat-list selector node-side first.
        let chatListSelector: string | null = 'div[aria-label="Chat Message List"]';
        if (!(await page.$(chatListSelector))) {
            const found = await findElementWithFallback(
                page,
                [],
                {
                    intent: 'Zoom in-meeting chat panel scroll container that holds the list of chat messages',
                    platform: 'ZOOM',
                    step: 'zoom.monitor.chatList',
                },
                { maxRetries: 1, delayMs: 100 },
            );
            chatListSelector = found ? found.selector : null;
        }
        if (chatListSelector) {
            console.log(`Listening for message changes via selector: ${chatListSelector}`);
            await page.evaluate((selector: string) => {
                const targetNode = document.querySelector(selector);
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
            }, chatListSelector);
        } else {
            console.log('No chat list selector available — skipping chat monitoring');
        }

        // Start transcription if enabled (LMA behavior)
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(page, details.startMessages);
            transcriptionService.startTranscription();
        }
        console.log('Waiting for meeting end.');
        let exitInfo: ExitInfo = { reason: 'unknown' };
        try {
            // Detect Zoom's own meeting-end UI: host ended the meeting, or the
            // VP was kicked. The dialog text differs between the two.
            const zoomDialogPromise: Promise<ExitInfo> = page
                .waitForFunction(
                    () => {
                        const buttons = document.querySelectorAll('button.zm-btn.zm-btn-legacy.zm-btn--primary.zm-btn__outline--blue');
                        for (const btn of buttons) {
                            const modal = btn.closest('.zm-modal, .zm-modal-legacy, .ReactModal__Content');
                            if (modal) {
                                const text = modal.textContent?.toLowerCase() || '';
                                if (text.includes('meeting has been ended') ||
                                    text.includes('meeting has ended') ||
                                    text.includes('meeting is end') ||
                                    text.includes('removed from the meeting') ||
                                    text.includes('have been removed')) {
                                    // Hand the matched dialog text back so we
                                    // can distinguish "host ended" from "you
                                    // were removed" without re-querying the DOM.
                                    return text;
                                }
                                continue;
                            }
                            return 'meeting ended';
                        }
                        return false;
                    },
                    // Playwright: 2nd positional is the page-function arg, options are 3rd.
                    undefined,
                    { timeout: details.meetingTimeout, polling: 1000 }
                )
                .then(async (handle) => {
                    const dialogText = (await handle.jsonValue()) as string;
                    const removed = dialogText.includes('removed');
                    return {
                        reason: removed ? 'removed-from-meeting' : 'host-ended',
                        trigger: 'ZOOM_END_DIALOG',
                    } as ExitInfo;
                })
                // Swallow Target-closed / timeout when this branch is the
                // race loser and the page goes away during cleanup —
                // otherwise it surfaces as an unhandled rejection. Log the
                // underlying error so unexpected mid-meeting aborts are diagnosable.
                .catch((err): ExitInfo => {
                    console.warn(`[zoom-end-watcher] waitForFunction rejected (page closed=${page.isClosed()}): ${err?.name ?? ''} ${err?.message ?? err}`);
                    return { reason: 'page-closed', trigger: 'ZOOM_END_DIALOG_ABORTED' };
                });

            exitInfo = await Promise.race([this.endRequested, zoomDialogPromise]);
        } catch (error) {
            console.log('Meeting timed out.');
            exitInfo = { reason: 'meeting-timeout', trigger: 'meetingTimeout' };
        } finally {
            details.start = false;
        }
        console.log(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
        return exitInfo;
    }
}
