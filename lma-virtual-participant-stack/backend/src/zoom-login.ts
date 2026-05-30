/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page, ElementHandle } from 'playwright-core';
import {
    SecretsManagerClient,
    GetSecretValueCommand,
} from '@aws-sdk/client-secrets-manager';
import {
    findElementWithFallback,
    analyzeUnknownDialog,
    resolveSelector,
    isResolverEnabled,
    analyzePageAction,
    scrollIntoViewAndClick,
} from './ai-dom-resolver.js';
import { details } from './details.js';

export interface ZoomCredentials {
    username: string;
    password: string;
    updatedAt?: string;
}

export type LoginOutcome = 'success' | 'manual-required' | 'invalid-credentials';

export interface LoginResult {
    outcome: LoginOutcome;
    detail?: string;
}

let secretsClient: SecretsManagerClient | null = null;
const getSecrets = (): SecretsManagerClient => {
    if (!secretsClient) {
        secretsClient = new SecretsManagerClient({
            region: process.env.AWS_REGION || 'us-east-1',
        });
    }
    return secretsClient;
};

export async function fetchZoomCredentials(secretName: string): Promise<ZoomCredentials> {
    const result = await getSecrets().send(
        new GetSecretValueCommand({ SecretId: secretName }),
    );
    if (!result.SecretString) {
        throw new Error(`Secret ${secretName} has no SecretString`);
    }
    const parsed = JSON.parse(result.SecretString) as ZoomCredentials;
    if (!parsed.username || !parsed.password) {
        throw new Error(`Secret ${secretName} missing username or password`);
    }
    return parsed;
}

const sleepJitter = (base: number, jitter: number): Promise<void> =>
    new Promise((r) => setTimeout(r, base + Math.random() * jitter));

// Race a browser-call promise against a wall-clock timeout. evaluate /
// type / click can hang for 30+ seconds when the page's execution context
// is destroyed mid-call (Zoom SPA re-mount); this turns the hang into a
// fast rejection so the surrounding retry/iteration loop can fire.
const withTimeout = async <T>(
    promise: Promise<T>,
    ms: number,
    label: string,
): Promise<T> => {
    let to: NodeJS.Timeout | undefined;
    try {
        return await Promise.race<T>([
            promise,
            new Promise<T>((_, reject) => {
                to = setTimeout(
                    () => reject(new Error(`[zoom-login] ${label} timed out after ${ms}ms`)),
                    ms,
                );
            }),
        ]);
    } finally {
        if (to) clearTimeout(to);
    }
};

const typeWithDelay = async (
    page: Page,
    elem: ElementHandle<Element>,
    text: string,
): Promise<void> => {
    // Focus in the DOM then type via page.keyboard (mirrors humanType in
    // zoom.ts): avoids Playwright's actionability check, which fails when
    // Zoom floats a transient overlay over the form. Budget scales with
    // text length so withTimeout doesn't abort legitimately-slow typing.
    const perChar = 50 + Math.floor(Math.random() * 70);
    const budget = perChar * Math.max(text.length, 1) * 3 + 2000;
    await withTimeout(
        (async () => {
            await elem.evaluate((el) => (el as HTMLElement).focus());
            await page.keyboard.type(text, { delay: perChar });
        })(),
        budget,
        `type(${text.length} chars)`,
    );
};

// Type into a field, then read back .value to confirm it landed. Zoom's
// SPA re-mounts the form mid-flow, so typing into a pre-hydration input
// silently drops the text and submits empty. On mismatch, relocate() the
// (re-mounted) element and retry once.
const typeAndVerify = async (
    page: Page,
    initialHandle: ElementHandle<Element>,
    text: string,
    relocate: () => Promise<ElementHandle<Element> | null>,
    label: string,
): Promise<{ ok: boolean; handle: ElementHandle<Element> }> => {
    const readValue = async (h: ElementHandle<Element>): Promise<string> => {
        try {
            return await withTimeout(
                h.evaluate((el: Element) => (el as HTMLInputElement).value || ''),
                3000,
                `${label} readValue`,
            );
        } catch {
            // Either the evaluate threw (destroyed context) or it timed
            // out. Either way the caller treats this as "verify failed"
            // and re-locates + retries.
            return '';
        }
    };
    const clearAndType = async (h: ElementHandle<Element>): Promise<void> => {
        try {
            await withTimeout(
                h.evaluate((el: Element) => {
                    const i = el as HTMLInputElement;
                    i.focus();
                    i.value = '';
                }),
                3000,
                `${label} clear`,
            );
        } catch {
            // best effort — if we can't clear, type() will append; the
            // verify step below catches the resulting mismatch.
        }
        await typeWithDelay(page, h, text);
    };

    let handle = initialHandle;
    for (let attempt = 1; attempt <= 2; attempt++) {
        await clearAndType(handle);
        await sleepJitter(150, 200);
        const got = await readValue(handle);
        if (got === text) {
            return { ok: true, handle };
        }
        console.warn(
            `[zoom-login] ${label} value mismatch after attempt ${attempt} ` +
                `(expected "${text.length}" chars, got "${got.length}" chars) — ` +
                `${attempt < 2 ? 're-locating and retrying' : 'giving up retry'}`,
        );
        if (attempt < 2) {
            const fresh = await relocate();
            if (!fresh) {
                console.warn(`[zoom-login] ${label} could not be re-located for retry`);
                return { ok: false, handle };
            }
            handle = fresh;
        }
    }
    return { ok: false, handle };
};

// Poll for the visible sign-in email input, returning the instant it
// appears (so a fast box pays no latency) or false at timeoutMs. The 30s
// default tolerates a CPU-starved cold start — Simli + Nova + Chrome +
// warmup contend on a 2-vCPU t3.medium, and Zoom's SPA can take 20-30s to
// hydrate. We poll the input directly rather than the URL, which can sit
// at /signin the whole time while the bundle parses.
const waitForSignInFormReady = async (
    page: Page,
    opts: { timeoutMs?: number } = {},
): Promise<boolean> => {
    const timeoutMs = opts.timeoutMs ?? 30_000;
    const deadline = Date.now() + timeoutMs;
    let polls = 0;
    while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 250));
        const hasInput = await withTimeout(
            page.evaluate(() => {
                const sels = ['#email', 'input[type="email"]', 'input[name="email"]'];
                for (const s of sels) {
                    const el = document.querySelector(s) as HTMLElement | null;
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
            }),
            2500,
            'waitForSignInFormReady probe',
        ).catch(() => false);
        polls += 1;
        if (hasInput) {
            console.log(
                `[zoom-login] sign-in email field ready after ${Date.now() - (deadline - timeoutMs)}ms (${polls} polls)`,
            );
            return true;
        }
    }
    console.warn(
        `[zoom-login] waitForSignInFormReady timed out after ${timeoutMs}ms at ${page.url()} (${polls} polls); proceeding anyway`,
    );
    return false;
};

/**
 * Sign in to Zoom on the web client. Resolves to:
 * - 'success' if we land on an authenticated Zoom page
 * - 'manual-required' if 2FA / CAPTCHA / risk challenge appears (caller is
 *   expected to have already escalated to MANUAL_ACTION_REQUIRED in the UI;
 *   we still wait up to `manualActionTimeoutMs` for the user to clear it)
 * - 'invalid-credentials' if the credentials are wrong
 */
const hasZoomAuthCookie = async (page: Page): Promise<boolean> => {
    // Zoom marks its session cookies as HttpOnly, so they're invisible to
    // `document.cookie` from JavaScript. Use the context's cookies() API
    // (CDP / Network.getCookies) which returns HttpOnly cookies.
    //
    // Only check cookies that are SET ONLY when authenticated. Verified
    // empirically against `curl -I https://zoom.us/signin`:
    //   - `_zm_ssid` (session id) is set even pre-login as an anonymous
    //     session token, so it's a false-positive
    //   - `cred` and `_zm_mtk_guid` are also unconditionally set
    //   - `zm_aid` (account id) and `zm_haid` (host account id) are
    //     ONLY set after authentication completes
    try {
        const cookies = await page.context().cookies(['https://zoom.us', 'https://app.zoom.us']);
        const wanted = new Set(['zm_aid', 'zm_haid']);
        return cookies.some((c) => wanted.has(c.name) && !!c.value);
    } catch {
        return false;
    }
};

export async function loginToZoom(
    page: Page,
    creds: ZoomCredentials,
    opts: { manualActionTimeoutMs?: number } = {},
): Promise<LoginResult> {
    const manualActionTimeoutMs = opts.manualActionTimeoutMs ?? 180_000;

    // Always go to /signin. If the persistent profile has a still-valid
    // session, Zoom will redirect /signin → /profile (or /myhome) and we
    // detect success without ever touching the email/password fields —
    // this is the cheapest and least-detection-prone path. If the session
    // is expired or missing, /signin shows the email form and we fall
    // through to the AI-driven sign-in loop.
    //
    // (Previously we probed /myhome first, which sometimes accepted a
    // stale session that wasn't actually trusted for meeting-join — the
    // user landed on /myhome but the meeting prejoin still got a bot-
    // detection block. Going to /signin and letting Zoom decide whether
    // we're authenticated is more reliable.)
    console.log('[zoom-login] Navigating to Zoom sign-in page');
    await page.goto('https://zoom.us/signin', { waitUntil: 'domcontentloaded' });
    await sleepJitter(800, 600);

    // If Zoom redirected us away from /signin, the saved session is still
    // good. Check for the auth cookie; if present, we're done.
    const postLoadUrl = page.url();
    const stillOnSignin = /zoom\.us\/(signin|login)/.test(postLoadUrl);
    if (!stillOnSignin && (await hasZoomAuthCookie(page))) {
        console.log(`[zoom-login] Saved session is valid (redirected to ${postLoadUrl}) — skipping sign-in`);
        return { outcome: 'success', detail: 'reused saved session' };
    }
    console.log(`[zoom-login] At ${postLoadUrl} — proceeding with email/password flow`);

    console.log('[zoom-login] step=waitForSignInFormReady BEGIN');
    const formReady = await waitForSignInFormReady(page);
    console.log(`[zoom-login] step=waitForSignInFormReady END (ready=${formReady}) at ${page.url()}`);

    // Step 1: email
    const emailPrimaries = ['#email', 'input[type="email"]', 'input[name="email"]'];
    const emailIntent = {
        intent: 'Zoom sign-in page email/username input field',
        platform: 'ZOOM' as const,
        step: 'zoom.login.email',
    };
    // When the field is already confirmed present, a short primary lookup
    // hits immediately; skip the AI fallback (3 Bedrock round-trips, ~40s
    // on a slow box). Only give the longer budget + AI when unconfirmed.
    console.log('[zoom-login] step=findEmail BEGIN');
    const emailRes = await findElementWithFallback(
        page,
        emailPrimaries,
        emailIntent,
        formReady ? { maxRetries: 4, delayMs: 400 } : { maxRetries: 8, delayMs: 750 },
    );
    console.log(`[zoom-login] step=findEmail END (found=${!!emailRes}, source=${emailRes?.source ?? 'none'})`);
    if (!emailRes) {
        return { outcome: 'manual-required', detail: 'Could not locate Zoom sign-in email field' };
    }
    console.log('[zoom-login] step=typeEmail BEGIN');
    const emailVerify = await typeAndVerify(
        page,
        emailRes.element,
        creds.username,
        async () => {
            const r = await findElementWithFallback(
                page,
                emailPrimaries,
                emailIntent,
                { maxRetries: 3, delayMs: 400 },
            );
            return r?.element ?? null;
        },
        'email',
    );
    console.log(`[zoom-login] step=typeEmail END (ok=${emailVerify.ok})`);
    if (!emailVerify.ok) {
        return {
            outcome: 'manual-required',
            detail: 'Could not reliably type email into Zoom sign-in field (form may be re-mounting).',
        };
    }
    await sleepJitter(150, 250);

    // After email is submitted, hand control to the AI navigator. It
    // looks at each successive page (with vision + DOM) and decides
    // whether to:
    //   - fill the password field (we provide creds.password)
    //   - skip an upsell / binding / "stay signed in?" interstitial
    //   - continue an informational page
    //   - wait (loading)
    //   - escalate to MANUAL_ACTION_REQUIRED for OTP / 2FA / CAPTCHA / SSO
    //   - declare done (auth-cookie set + at destination)
    //
    // This replaces the old deterministic email→Next→password→Sign In
    // flow which broke whenever Zoom inserted a new step (passkey
    // binding, phone binding, OTP challenge for unusual logins, etc.).
    const startedAt = Date.now();
    let didFillPassword = false;

    // First click "Next" to advance from email step. AI handles everything
    // afterwards including potentially filling password.
    console.log('[zoom-login] step=findNext BEGIN');
    const nextRes = await findElementWithFallback(
        page,
        [
            'button[type="submit"]',
            '#js_btn_login',
            'button[aria-label="Next"]',
            'button[aria-label="Sign In"]',
        ],
        {
            intent: 'Zoom sign-in "Next" / "Continue" button shown after entering the email but before the password field is revealed (do NOT pick a SSO/Google/Apple/Facebook sign-in button)',
            platform: 'ZOOM',
            step: 'zoom.login.next',
        },
        { maxRetries: 6, delayMs: 500 },
    );
    console.log(`[zoom-login] step=findNext END (found=${!!nextRes}, source=${nextRes?.source ?? 'none'})`);
    if (nextRes) {
        console.log('[zoom-login] step=clickNext BEGIN');
        await withTimeout(nextRes.element.click(), 5000, 'Next button click').catch((err) => {
            console.warn(`[zoom-login] Next button click failed/timed out: ${err?.message || err}`);
        });
        console.log('[zoom-login] step=clickNext END');
        await sleepJitter(800, 800);
    }
    // If there was no Next button, the page might already be showing the
    // password field directly (one-step flow) or have already advanced —
    // the AI navigator below will figure it out either way.

    return await aiDrivenLoginLoop(page, creds, manualActionTimeoutMs, startedAt, didFillPassword);
}

/**
 * The AI-driven part of the login flow. Called after email + Next. Each
 * iteration sends Claude a screenshot + visible-element snapshot of the
 * current page and picks one of: fill_password, skip, continue, wait,
 * needs_human, done. Caps password fills to 1 to avoid re-typing on a
 * loop. Uses fast-path auth-cookie / invalid-creds / OTP-page
 * checks as fast-path successes / escalations between iterations.
 */
async function aiDrivenLoginLoop(
    page: Page,
    creds: ZoomCredentials,
    timeoutMs: number,
    startedAt: number,
    initialDidFillPassword: boolean,
): Promise<LoginResult> {
    const ITER_PAUSE_MS = 1500;
    // 10 iterations covers the happy path with wait-state padding: each
    // real action (fill_password, click Sign-In, skip phone-binding) is
    // typically followed by a "wait" iteration while Zoom transitions
    // pages. So 4 real actions can consume up to 8 iterations. The
    // stuck-loop detector below handles the misbehaving case (same
    // selector twice without page change) so we don't need a tight cap
    // here; this just prevents runaway loops if both detectors miss.
    const MAX_ITERATIONS = 10;
    let didFillPassword = initialDidFillPassword;
    let escalated = false;
    // Stuck-loop detector: if Claude returns the same action signature
    // twice in a row AND the page hasn't changed, we're going in circles
    // and should bail to manual-action rather than burn iterations and
    // Bedrock spend.
    let lastActionSignature: string | null = null;
    let lastPageFingerprint: string | null = null;

    const isNavError = (e: any): boolean => {
        const m = (e?.message || '').toString();
        return (
            m.includes('Execution context was destroyed') ||
            m.includes('Target closed') ||
            m.includes('frame got detached') ||
            m.includes('Navigation timeout')
        );
    };

    for (let i = 0; i < MAX_ITERATIONS; i++) {
        if (page.isClosed()) return { outcome: 'manual-required', detail: 'page closed' };
        if (Date.now() - startedAt > timeoutMs) {
            return { outcome: 'manual-required', detail: 'AI-driven login loop timed out' };
        }

        // Fast path 1: auth cookie set → we're in. Try to dismiss any
        // visible post-login interstitial then declare success.
        if (await hasZoomAuthCookie(page)) {
            await dismissPostLoginInterstitials(page);
            if (escalated && details.invite.virtualParticipantId) {
                const { createStatusManager } = await import('./status-manager.js');
                await createStatusManager(details.invite.virtualParticipantId).clearManualAction();
            }
            return { outcome: 'success' };
        }

        // Fast path 2: invalid credentials.
        try {
            const invalid = await page.evaluate(() => {
                const text = (document.body?.innerText || '').toLowerCase();
                return (
                    text.includes('incorrect email or password') ||
                    text.includes('incorrect password') ||
                    text.includes('invalid password') ||
                    text.includes("doesn't match our records") ||
                    text.includes('account does not exist') ||
                    text.includes('unable to sign in')
                );
            });
            if (invalid) {
                return { outcome: 'invalid-credentials', detail: 'Zoom rejected the credentials' };
            }
        } catch (e) {
            if (!isNavError(e)) throw e;
            await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
            continue;
        }

        // Main path: ask Claude what to do.
        let action: any = null;
        try {
            action = await analyzePageAction(page, {
                platform: 'ZOOM',
                useScreenshot: true,
                allowFillPassword: !didFillPassword,
            });
        } catch (e) {
            if (!isNavError(e)) {
                console.warn('[zoom-login] analyzePageAction error:', e);
            }
            await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
            continue;
        }

        if (!action) {
            // No decision returned — sleep and retry.
            await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
            continue;
        }

        console.log(
            `[zoom-login] AI iter ${i + 1}: ${action.kind} (${action.reason}${
                action.selector ? `; selector="${action.selector}"` : ''
            })`,
        );

        // Stuck-loop guard: if Claude returned the same action signature
        // last iteration AND the page still looks the same, our action
        // didn't actually change the page (e.g. selector missed, click
        // got swallowed, page state machine isn't advancing). Bail rather
        // than hammer the same dead-end.
        const actionSignature = `${action.kind}|${action.selector || ''}`;
        let pageFingerprint = '';
        try {
            pageFingerprint = await page.evaluate(() => {
                const sels = ['button', 'a', 'input', '[role="button"]'];
                const ids: string[] = [];
                for (const s of sels) {
                    document.querySelectorAll(s).forEach((el) => {
                        const e = el as HTMLElement;
                        const r = e.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return;
                        ids.push(`${e.tagName}#${e.id || ''}.${e.className || ''}`);
                    });
                }
                return `${location.href}::${ids.slice(0, 30).join(',')}`;
            });
        } catch {
            // ignore — we'll just skip the stuck-loop guard this iter
        }
        if (
            action.kind !== 'wait' &&
            actionSignature === lastActionSignature &&
            pageFingerprint &&
            pageFingerprint === lastPageFingerprint
        ) {
            console.warn(
                `[zoom-login] Stuck loop detected (action "${actionSignature}" repeated without page change) — escalating`,
            );
            return {
                outcome: 'manual-required',
                detail: `Sign-in is stuck: action "${action.kind}" on selector "${action.selector || '(none)'}" did not advance the page.`,
            };
        }
        lastActionSignature = actionSignature;
        lastPageFingerprint = pageFingerprint;

        if (action.kind === 'done') {
            return { outcome: 'success' };
        }
        if (action.kind === 'wait') {
            await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
            continue;
        }
        if (action.kind === 'needs_human') {
            // Escalate to MANUAL_ACTION_REQUIRED so the user knows what to do,
            // then switch to a cheap cookie-only poll instead of continuing
            // to call Bedrock + screenshot every iteration. The user might
            // take a few minutes to solve a CAPTCHA / 2FA / SSO challenge,
            // and there's no point re-asking Claude what to do during that
            // window — the only thing we need to detect is "auth cookie now
            // present", which is a free DOM read.
            if (details.invite.virtualParticipantId) {
                const { createStatusManager } = await import('./status-manager.js');
                await createStatusManager(details.invite.virtualParticipantId).setManualActionRequired(
                    'LOGIN',
                    action.reason ||
                        'Zoom sign-in needs your help: please complete the verification step in the LMA viewer.',
                    Math.floor(timeoutMs / 1000),
                );
            }
            const POLL_MS = 5000;
            const remainingMs = Math.max(0, timeoutMs - (Date.now() - startedAt));
            const deadline = Date.now() + remainingMs;
            while (Date.now() < deadline) {
                await new Promise((r) => setTimeout(r, POLL_MS));
                if (page.isClosed()) {
                    return { outcome: 'manual-required', detail: 'page closed during manual-action wait' };
                }
                if (await hasZoomAuthCookie(page)) {
                    if (details.invite.virtualParticipantId) {
                        const { createStatusManager } = await import('./status-manager.js');
                        await createStatusManager(details.invite.virtualParticipantId).clearManualAction();
                    }
                    await dismissPostLoginInterstitials(page);
                    return { outcome: 'success', detail: 'human completed sign-in' };
                }
            }
            return { outcome: 'manual-required', detail: 'human did not complete sign-in within timeout' };
        }
        if (action.kind === 'fill_password' && !didFillPassword && action.selector) {
            const passwordSelector = action.selector;
            const handle = await page.$(passwordSelector);
            if (handle) {
                try {
                    await handle.evaluate((el: Element) => {
                        (el as HTMLElement).scrollIntoView({ block: 'center' });
                    });
                    const pwVerify = await typeAndVerify(
                        page,
                        handle as ElementHandle<Element>,
                        creds.password,
                        async () => page.$(passwordSelector),
                        'password',
                    );
                    if (!pwVerify.ok) {
                        return {
                            outcome: 'manual-required',
                            detail: 'Could not reliably type password into Zoom sign-in field (form may be re-mounting).',
                        };
                    }
                    didFillPassword = true;
                    await sleepJitter(150, 250);
                    // Submit the form. Prefer the AI's submitSelector; fall
                    // back to common Sign-In button selectors and finally to
                    // pressing Enter — otherwise the password sits in the
                    // input box and subsequent iterations can't see we're
                    // mid-flight (allowFillPassword goes false after the
                    // first fill, so without a submit the loop can't recover).
                    const submitCandidates = [
                        action.submitSelector,
                        '#js_btn_login',
                        'button[type="submit"]',
                        'input[type="submit"]',
                        'button.signin-btn',
                    ].filter((s): s is string => !!s);
                    let submitted = false;
                    for (const sel of submitCandidates) {
                        if (await scrollAndClick(page, sel)) {
                            submitted = true;
                            break;
                        }
                    }
                    if (!submitted) {
                        await (pwVerify.handle as any).press('Enter').catch(() => null);
                    }
                    await page
                        .waitForNavigation({ timeout: 15_000, waitUntil: 'domcontentloaded' })
                        .catch(() => null);
                } catch (err) {
                    console.warn('[zoom-login] fill_password failed:', err);
                }
            }
            await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
            continue;
        }
        if ((action.kind === 'skip' || action.kind === 'continue') && action.selector) {
            await scrollAndClick(page, action.selector);
            await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
            continue;
        }

        // Unknown / unhandled action — pause and retry.
        await new Promise((r) => setTimeout(r, ITER_PAUSE_MS));
    }

    return { outcome: 'manual-required', detail: 'AI-driven login loop exhausted iterations' };
}

async function scrollAndClick(page: Page, selector: string): Promise<boolean> {
    try {
        const ok = await scrollIntoViewAndClick(page, selector);
        return ok;
    } catch {
        return false;
    }
}

/**
 * AI-first navigation through Zoom's post-login interstitials (passkey,
 * phone/SMS binding, browser-extension promo, "stay signed in?", etc.).
 *
 * Strategy: every iteration, ask Claude (with a screenshot + visible
 * interactive elements) what to do on the current page:
 *   - skip / continue → click the suggested element (auto-scrolled into
 *     view) and re-evaluate.
 *   - wait → loading state, sleep and re-evaluate.
 *   - done → we've reached an authenticated destination; stop.
 *   - needs_human → escalate (caller decides what to do; this helper
 *     just returns; aiDrivenLoginLoop handles escalation).
 *
 * Falls back to a regex match on visible "Skip / Not now / Maybe later"
 * link text when the AI fails to respond (e.g. Bedrock outage). Robust
 * to new Zoom upsell pages without code changes.
 *
 * Returns true if we appear to have reached a destination ('done'),
 * false if we hit max iterations or got 'needs_human' / unrecoverable error.
 */
export async function dismissPostLoginInterstitials(
    page: Page,
    opts: { maxIterations?: number; perIterationMs?: number } = {},
): Promise<boolean> {
    const maxIterations = opts.maxIterations ?? 8;
    const perIterationMs = opts.perIterationMs ?? 1500;

    const isNavError = (e: any): boolean => {
        const m = (e?.message || '').toString();
        return (
            m.includes('Execution context was destroyed') ||
            m.includes('Target closed') ||
            m.includes('frame got detached') ||
            m.includes('Navigation timeout')
        );
    };

    for (let i = 0; i < maxIterations; i++) {
        if (page.isClosed()) return false;
        const url = page.url();
        // Quick early-out: not a /signin sub-route → nothing to dismiss.
        const onSignin = /zoom\.us\/signin/.test(url) || /zoom\.us\/login/.test(url);
        if (!onSignin) {
            console.log(`[zoom-login] Post-login navigator: reached non-signin URL ${url}`);
            return true;
        }

        // Layer 1 (preferred): AI page-action analyzer with screenshot.
        // Vision lets Claude understand pages that are visually obvious
        // but DOM-ambiguous (e.g. a phone-binding form with the inputs
        // scrolled off-screen).
        if (isResolverEnabled()) {
            try {
                const action = await analyzePageAction(page, {
                    platform: 'ZOOM',
                    useScreenshot: true,
                });
                if (action) {
                    console.log(
                        `[zoom-login] AI page-action #${i + 1}: ${action.kind} (${action.reason}${
                            action.selector ? `; selector="${action.selector}"` : ''
                        })`,
                    );
                    if (action.kind === 'done') return true;
                    if (action.kind === 'needs_human') return false;
                    if (action.kind === 'wait') {
                        await new Promise((r) => setTimeout(r, perIterationMs));
                        continue;
                    }
                    if ((action.kind === 'skip' || action.kind === 'continue') && action.selector) {
                        const clicked = await scrollIntoViewAndClick(page, action.selector);
                        if (clicked) {
                            await new Promise((r) => setTimeout(r, perIterationMs));
                            continue;
                        }
                        console.warn(
                            `[zoom-login] AI suggested ${action.kind} via "${action.selector}" but the click failed; trying regex fallback.`,
                        );
                    }
                }
            } catch (err: any) {
                if (isNavError(err)) {
                    await new Promise((r) => setTimeout(r, perIterationMs));
                    continue;
                }
                console.warn('[zoom-login] AI page-action analyzer error (will fall back to regex):', err);
            }
        }

        // Layer 2 (fallback): regex on visible link text. Used when AI
        // is unavailable (Bedrock outage, RESOLVER_DISABLED) or when AI
        // returned a selector that didn't click. Cheap and deterministic.
        let dismissedBy: string | null = null;
        try {
            dismissedBy = await page.evaluate(() => {
                const tags = ['a', 'button'];
                const wantText =
                    /^\s*(skip( for now| this step| and continue| and sign in)?|not now|maybe later|no thanks?|don'?t ask( again)?|cancel|continue without|do this later|i'?ll do this later|remind me later|not interested|don'?t ask me again)\s*\.?\s*$/i;
                for (const tag of tags) {
                    for (const el of Array.from(document.querySelectorAll(tag))) {
                        const t = (el.textContent || '').trim();
                        if (!t) continue;
                        if (!wantText.test(t)) continue;
                        const he = el as HTMLElement;
                        he.scrollIntoView({ block: 'center', inline: 'center' });
                        const rect = he.getBoundingClientRect?.();
                        if (!rect || rect.width === 0 || rect.height === 0) continue;
                        const style = window.getComputedStyle(he);
                        if (style.visibility === 'hidden' || style.display === 'none') continue;
                        he.click();
                        return t;
                    }
                }
                return null;
            });
        } catch (err: any) {
            if (isNavError(err)) {
                await new Promise((r) => setTimeout(r, perIterationMs));
                continue;
            }
            console.warn('[zoom-login] dismissPostLoginInterstitials regex error:', err);
            return false;
        }

        if (dismissedBy) {
            console.log(`[zoom-login] Regex fallback dismissed via "${dismissedBy}" (iteration ${i + 1})`);
            await new Promise((r) => setTimeout(r, perIterationMs));
            continue;
        }

        // Nothing matched at either layer.
        console.log(`[zoom-login] No dismissable interstitial found at ${url} (iteration ${i + 1})`);
        return false;
    }
    console.log(`[zoom-login] dismissPostLoginInterstitials: hit max iterations (${maxIterations})`);
    return false;
}

