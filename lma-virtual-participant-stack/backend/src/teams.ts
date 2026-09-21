/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page } from 'playwright-core';

import { details, matchesEndCommand, exitMessagesFor, ExitInfo, ExitReasonCode, MeetingInitOptions } from "./details.js";
import { transcriptionService } from "./scribe.js";
import { createStatusManager } from "./status-manager.js";
import { voiceAssistant } from './voice-assistant.js';
import { simliAvatar } from './simli-avatar.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { findElementWithFallback, classifyJoinState, isResolverEnabled } from './ai-dom-resolver.js';
import { startDialogWatchdog } from './dialog-watchdog.js';
import { humanClick, humanType } from './prejoin-actions.js';
import { gotoMeetingPage, MEETING_HOST_PATTERNS } from './meeting-navigation.js';

/** What one poll of the Teams roster badge told us. */
export type AttendeeReading =
    | { state: 'OK'; count: number }
    | { state: 'BADGE_MISSING' }
    /**
     * The badge could not be read, but the page still shows in-meeting chrome.
     * Evidence of a live meeting, so it vetoes the miss — for a bounded number of
     * consecutive polls, since Teams does not reliably remove that chrome when a
     * meeting ends. See `decideAttendeeAction`.
     */
    | { state: 'BADGE_MISSING_IN_MEETING' };

/** Debounced counters carried across polls. */
export interface AttendeeWatchdogState {
    consecutiveLonely: number;
    consecutiveMissing: number;
    /**
     * Missing-badge polls vetoed by in-meeting evidence. Load-bearing, not just
     * diagnostic: it is what spends the veto allowance.
     */
    suppressedMissing: number;
    /**
     * Consecutive polls with no readable badge, however they were classified.
     *
     * The backstop of last resort, and the only counter a veto cannot reset. The
     * other two are individually bounded, but a veto clears `consecutiveMissing`,
     * so an alternating sequence — a run of misses, then one poll where the chrome
     * happens to be visible — multiplies the two bounds together instead of adding
     * them: 14 misses per veto reaches 7.5 HOURS at the default cadence, which is
     * the #540 outcome by another route. This counter makes the ceiling additive
     * again, whatever the interleaving.
     */
    unreadableRun: number;
}

/** Tunables for the Teams attendee watchdog, resolved once per meeting. */
export interface AttendeeWatchdogConfig {
    /** Cadence of the watchdog poll, in milliseconds. */
    pollMs: number;
    /** Consecutive genuine "<=1 attendee" readings before the meeting ends. */
    pollsBeforeEnd: number;
    /** Consecutive ABSENT-badge readings before the meeting ends. */
    pollsBeforeEndMissing: number;
    /**
     * How many consecutive polls in-meeting chrome may veto an absent badge
     * before the veto is spent and the absent badge counts again.
     *
     * The veto must be finite. Teams does not reliably remove its in-meeting
     * controls when a meeting ends: in the incident behind GitHub #540 the
     * hang-up button was still present and visible afterwards — that is exactly
     * why the `HANGUP_BUTTON_HIDDEN` watch, armed for the whole meeting, never
     * fired there. An unbounded veto would therefore restore #540, with the VP
     * recording video and holding a voice session until the four-hour meeting
     * timeout. Spending the veto keeps a long content share safe while leaving a
     * real backstop behind it.
     */
    maxSuppressedPolls: number;
}

/**
 * Defaults for the attendee watchdog.
 *
 * The two bounds are deliberately different sizes, and the asymmetry is the
 * whole point. A badge that is PRESENT and reads <=1 is a direct measurement:
 * ~60s of it means everyone really has gone. A badge that is ABSENT measures
 * nothing — Teams drops the roster button (and with it the count badge) from the
 * DOM whenever the meeting toolbar collapses, which happens routinely during
 * content share and full-screen layouts. A ~40s bound on that reading ended live
 * meetings mid-sentence while people were still speaking (GitHub #660), so the
 * missing-badge bound is minutes wide.
 *
 * A wide bound alone is not enough, though: nothing in the VP moves the pointer
 * once it is in the meeting, so a toolbar that auto-hides during a share can stay
 * hidden for as long as the share lasts — longer than any bound worth setting.
 * That is why an unreadable badge is first corroborated against the rest of the
 * in-meeting chrome (see `decideAttendeeAction`), and the bound below applies to
 * the case where the badge is unreadable AND that chrome is gone too, which is the
 * shape of a meeting that has actually ended.
 *
 * `maxSuppressedPolls` then bounds the corroboration itself, because the chrome
 * is not a reliable end-of-meeting signal either — see the field docs above. The
 * worst case in either direction is therefore finite: a share is safe for ~30
 * minutes at a stretch, and a meeting whose end Teams announces to nobody is left
 * after ~35 at the default cadence. Both windows are capped at 30 minutes
 * independently, so the ceiling is an hour at the slowest cadence and never more.
 */
export const DEFAULT_ATTENDEE_WATCHDOG_CONFIG: AttendeeWatchdogConfig = {
    pollMs: 20_000,
    pollsBeforeEnd: 3, // ~60s
    pollsBeforeEndMissing: 15, // ~5 min
    maxSuppressedPolls: 90, // ~30 min of content share
};

/**
 * Hard limits on the tunables, so no setting — mistyped or merely unwise — can be
 * worse than either bug this watchdog exists to prevent.
 *
 * `pollMs` is capped well below 2^31-1 because Node clamps a `setInterval` delay
 * above that to 1ms: an accidental extra digit would otherwise turn the watchdog
 * into a hot loop that hammers `page.evaluate` and burns through the whole
 * missing-badge bound in milliseconds. The poll counts are capped so that a
 * single extra zero cannot leave the VP effectively unbounded — the #540 shape,
 * where a VP held a voice session and uploaded video to the 8-hour ceiling.
 */
const WATCHDOG_LIMITS = {
    pollMs: { minimum: 1_000, maximum: 120_000 },
    polls: { minimum: 1, maximum: 90 },
    /**
     * Bounds on the WALL-CLOCK tolerance, not just on the individual values.
     * Both counts can be in range while their product is not, and the product is
     * what decides how long a VP outlives its meeting — or how short a share it
     * survives. The floor matters as much as the ceiling: at the documented
     * minimum cadence of 1000ms, 15 polls is 15 seconds, which is tighter than
     * the ~40s that caused GitHub #660 in the first place.
     */
    missingToleranceMs: { minimum: 4 * 60 * 1000, maximum: 30 * 60 * 1000 },
    /**
     * Bounds on how long an empty-but-readable roster is tolerated. The floor is
     * what stops `VPPollsBeforeEnd=1` at the minimum cadence from becoming a
     * one-second debounce — the #317/#318 shape, reachable from the parameters
     * page.
     */
    aloneToleranceMs: { minimum: 30 * 1000, maximum: 10 * 60 * 1000 },
    /** Ceiling on how long in-meeting chrome may veto an absent badge. */
    maxSuppressionMs: 30 * 60 * 1000,
} as const;

/**
 * Convert a wall-clock duration to whole polls at this cadence, at least one.
 *
 * Rounds in the direction that keeps the stated bound true: up when the duration is
 * a floor, down when it is a ceiling. `Math.round` for both would let a floor of
 * four minutes resolve to 3.87 and a ceiling of thirty to 30.97.
 */
const pollsFor = (ms: number, pollMs: number, bound: 'floor' | 'ceiling'): number =>
    Math.max(1, bound === 'floor' ? Math.ceil(ms / pollMs) : Math.floor(ms / pollMs));

/**
 * Read one integer tunable from the environment, falling back to the default
 * when it is unset, unparseable, or out of range.
 *
 * Operators need to be able to adjust these on a deployment that is hitting an
 * unusual Teams layout without rebuilding the VP container image, so they are
 * environment variables (fed by CloudFormation parameters) rather than baked-in
 * constants. Only plain digit strings are accepted: `Number()` alone would also
 * take `0x10`, `2e4` and `20000.0`, and a value that silently means something
 * other than it reads is worse here than a rejected one.
 */
function intFromEnv(
    raw: string | undefined,
    fallback: number,
    { minimum, maximum }: { minimum: number; maximum: number },
    variable: string,
): number {
    if (raw === undefined || raw.trim() === '') return fallback;
    const trimmed = raw.trim();
    const parsed = /^\d+$/.test(trimmed) ? Number(trimmed) : NaN;
    if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
        console.log(
            `Ignoring ${variable}="${raw}" ` +
                `(want a whole number from ${minimum} to ${maximum}) — using ${fallback}`,
        );
        return fallback;
    }
    return parsed;
}

/**
 * Hold a poll count inside a wall-clock window, and say so when it moves.
 *
 * Each value is individually range-checked already; the DURATION a count produces
 * at the configured cadence may still be out of bounds in either direction, and
 * that duration is what actually decides the behaviour. Both directions are logged
 * because an operator who sets 90 and gets 5 otherwise has nothing to explain it.
 */
function clampPolls(
    requested: number,
    pollMs: number,
    window: { minimum: number; maximum: number },
    variable: string,
): number {
    const resolved = Math.min(
        Math.max(requested, pollsFor(window.minimum, pollMs, 'floor')),
        pollsFor(window.maximum, pollMs, 'ceiling'),
    );
    if (resolved !== requested) {
        console.log(
            `Using ${resolved} polls for ${variable}, not ${requested}: at ${pollMs}ms per poll ` +
                `that keeps the tolerance between ${window.minimum}ms and ${window.maximum}ms`,
        );
    }
    return resolved;
}

/** Resolve the watchdog tunables from the VP task definition's environment. */
export function resolveAttendeeWatchdogConfig(
    env: Record<string, string | undefined> = process.env,
): AttendeeWatchdogConfig {
    const pollMs = intFromEnv(
        env.VP_ATTENDEE_POLL_MS,
        DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollMs,
        WATCHDOG_LIMITS.pollMs,
        'VP_ATTENDEE_POLL_MS',
    );
    // Bounded by wall clock in both directions, for the same reasons as the missing
    // bound below: 90 polls is three hours at the slowest cadence, and 1 poll is one
    // second at the fastest.
    const requestedAlone = intFromEnv(
        env.VP_POLLS_BEFORE_END,
        DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollsBeforeEnd,
        WATCHDOG_LIMITS.polls,
        'VP_POLLS_BEFORE_END',
    );
    const pollsBeforeEnd = clampPolls(requestedAlone, pollMs, WATCHDOG_LIMITS.aloneToleranceMs, 'VP_POLLS_BEFORE_END');
    const requestedMissing = intFromEnv(
        env.VP_POLLS_BEFORE_END_MISSING,
        DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollsBeforeEndMissing,
        WATCHDOG_LIMITS.polls,
        'VP_POLLS_BEFORE_END_MISSING',
    );
    const pollsBeforeEndMissing = clampPolls(
        requestedMissing,
        pollMs,
        WATCHDOG_LIMITS.missingToleranceMs,
        'VP_POLLS_BEFORE_END_MISSING',
    );
    return {
        pollMs,
        pollsBeforeEnd,
        pollsBeforeEndMissing,
        maxSuppressedPolls: pollsFor(WATCHDOG_LIMITS.maxSuppressionMs, pollMs, 'ceiling'),
    };
}

export type AttendeeDecision =
    | { action: 'continue' }
    | { action: 'end'; reason: ExitReasonCode; trigger: string; detail: string };

/**
 * Teams' pre-join display-name field validates against a fixed character set —
 * "letters, numbers, spaces, and these symbols: - ' . _ @" — and the default
 * identity template `LMA ({LMA_USER})` fails it on the parentheses, blocking
 * the join. Rewrite `LMA (user)` as `LMA - user` and strip anything else the
 * field would reject. Teams-only: chat messages and every other platform
 * accept the parenthesised form, so the identity itself is left alone.
 */
export function sanitizeTeamsDisplayName(name: string): string {
    const cleaned = name
        .replace(/\s*\(/g, ' - ')
        .replace(/\)/g, '')
        // eslint-disable-next-line no-useless-escape
        .replace(/[^A-Za-z0-9 \-'._@]/g, '')
        .replace(/\s{2,}/g, ' ')
        .trim();
    return /[A-Za-z0-9]/.test(cleaned) ? cleaned : 'LMA';
}

/**
 * Decide whether the meeting has ended, from one badge reading plus the running
 * counters.
 *
 * Extracted from the watchdog's setInterval so the decision is unit-testable —
 * the bug this fixes lived in a closure that no test could reach.
 *
 * BADGE_MISSING is the important case, and it has been wrong in both directions.
 * Teams removes the roster badge when the host ends the meeting, so a missing
 * badge is the *expected* state for the very event the watchdog exists to detect;
 * treating it as "unknown, never leave" meant no number of missing reads could
 * ever end the meeting, and the VP recorded video and held a voice session to the
 * 8-hour MicroVM ceiling (GitHub #540). But Teams also drops the badge during
 * ordinary in-meeting layouts, so a tight bound on it ends live meetings
 * mid-sentence (GitHub #317, #318, #660).
 *
 * A wider bound is not sufficient on its own, because the hidden toolbar that
 * takes the badge with it can stay hidden for the whole of a screen share. So the
 * caller corroborates an unreadable badge against the in-meeting chrome and
 * reports BADGE_MISSING_IN_MEETING when it is still there. That reading is
 * treated as evidence of a live meeting and resets the missing counter.
 *
 * The veto is SPENT rather than granted indefinitely, and that limit is the
 * safeguard against the opposite failure. Teams does not reliably tear its
 * in-meeting controls down when a meeting ends — in the incident behind #540 the
 * hang-up button was still present and visible afterwards, which is why the
 * `HANGUP_BUTTON_HIDDEN` watch never fired there — so "chrome is present" cannot
 * be trusted as proof of a live meeting for an unbounded time. After
 * `maxSuppressedPolls` consecutive vetoes the reading is treated as a plain miss
 * and the ordinary bound applies again. A readable badge resets the allowance, so
 * it is consecutive suppression that is limited, not suppression over the whole
 * meeting.
 *
 * `unreadableRun` closes the gap that leaves: because a veto clears
 * `consecutiveMissing`, an alternating sequence would otherwise multiply the two
 * bounds instead of adding them (14 misses per veto reached 7.5 hours). The run
 * counter ends the meeting at allowance + bound however the polls are classified.
 *
 * Note the corroboration can only ever make the watchdog more patient. If it is
 * wrong in the conservative direction — no chrome found while the meeting is in
 * fact live — the reading degrades to plain BADGE_MISSING and the wide bound
 * still applies, which is no worse than having no corroboration at all.
 *
 * The lonely and missing counters remain mutually exclusive: a reading is a
 * genuine count, a corroborated miss, or an uncorroborated miss, and each resets
 * the others.
 */
export function decideAttendeeAction(
    reading: AttendeeReading,
    state: AttendeeWatchdogState,
    config: AttendeeWatchdogConfig = DEFAULT_ATTENDEE_WATCHDOG_CONFIG,
): AttendeeDecision {
    if (reading.state !== 'OK') {
        state.unreadableRun += 1;
        // Ceiling on the whole unreadable run, regardless of how it was classified
        // poll by poll. Without this the veto's reset of `consecutiveMissing` makes
        // the two bounds multiply rather than add.
        const runCeiling = config.maxSuppressedPolls + config.pollsBeforeEndMissing;
        if (state.unreadableRun >= runCeiling) {
            return {
                action: 'end',
                reason: 'removed-from-meeting',
                trigger: 'attendee-badge-missing',
                detail:
                    `no readable attendee count for ${state.unreadableRun} consecutive polls ` +
                    `(the ${config.maxSuppressedPolls}-poll in-meeting allowance is spent) — ` +
                    'the meeting has ended or the VP was removed',
            };
        }
    }

    if (reading.state === 'BADGE_MISSING_IN_MEETING') {
        state.suppressedMissing += 1;
        if (state.suppressedMissing <= config.maxSuppressedPolls) {
            state.consecutiveMissing = 0;
            // No count was read, so there is nothing to say about being alone —
            // don't let a share-hidden roster accrue toward the lonely exit either.
            state.consecutiveLonely = 0;
            return { action: 'continue' };
        }
        // Allowance spent. Chrome that has been present with no readable roster
        // for this long is no longer good evidence of a live meeting, so fall
        // through and count the reading as the plain miss it otherwise is.
    }

    if (reading.state !== 'OK') {
        state.consecutiveMissing += 1;
        // Any unreadable poll clears the lonely run: an unreadable badge is not
        // evidence about how many people are present. Note this means an
        // alternating readable-alone / unreadable sequence never reaches the lonely
        // exit — as on develop — but `unreadableRun` above bounds it regardless.
        state.consecutiveLonely = 0;
        if (state.consecutiveMissing >= config.pollsBeforeEndMissing) {
            const vetoed = state.suppressedMissing > 0
                ? ` (in-meeting chrome vetoed ${Math.min(state.suppressedMissing, config.maxSuppressedPolls)} earlier polls)`
                : '';
            return {
                action: 'end',
                reason: 'removed-from-meeting',
                trigger: 'attendee-badge-missing',
                detail:
                    `no readable attendee count for ${state.consecutiveMissing} consecutive polls` +
                    `${vetoed} — the meeting has ended or the VP was removed`,
            };
        }
        return { action: 'continue' };
    }

    // A readable badge is unambiguous, so it restores the suppression allowance and
    // clears both miss counters: a meeting with several shares in it gets a fresh
    // allowance for each, while a single unbroken run of unreadable polls stays
    // bounded however those polls were classified.
    state.suppressedMissing = 0;
    state.consecutiveMissing = 0;
    state.unreadableRun = 0;
    if (reading.count > 1) {
        state.consecutiveLonely = 0;
        return { action: 'continue' };
    }

    state.consecutiveLonely += 1;
    if (state.consecutiveLonely >= config.pollsBeforeEnd) {
        return {
            action: 'end',
            reason: 'alone-in-meeting',
            trigger: 'attendees-left',
            detail: `count<=1 for ${state.consecutiveLonely} consecutive polls`,
        };
    }
    return { action: 'continue' };
}

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

    // Route one incoming chat message through the command logic. Called by the
    // message poll loop (previously an exposed `messageChange` function fed by a
    // MutationObserver — replaced by polling because the anon chat list is
    // virtualized and the v2 message-node shape doesn't exist there). Handles:
    // end command ("LMA leave/end"), pause/resume, and transcript capture.
    // Teams chat text doesn't expose the sender on the same node, so we use the
    // lenient matcher and a generic farewell.
    private async handleIncomingMessage(page: Page, message: string): Promise<void> {
        if (!message) return;
        if (matchesEndCommand(message)) {
            console.log(`LMA Virtual Participant has been asked to leave the meeting: ${JSON.stringify(message)}`);
            try {
                await this.sendMessages(page, exitMessagesFor(null));
            } catch (e) {
                // Best effort — fall through to ending the meeting.
                console.warn('Could not send goodbye message:', e);
            }
            details.start = false;
            // Hand off to the wait-for-meeting-end race; the orchestrator's
            // cleanup chain in index.ts owns the browser close.
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
        return (await this.inMeetingSignal(page)) !== null;
    }

    /**
     * As `isInMeeting`, but returns the selector that matched (or null).
     *
     * The attendee watchdog uses this rather than the boolean because "some
     * in-meeting chrome is present" is the reason it keeps a VP in a meeting whose
     * roster it cannot read, and when that turns out to be wrong the one thing
     * needed to diagnose it is WHICH of these signals was still there. Left/right
     * rail buttons on the full client, for instance, outlive a meeting in a way
     * the meeting stage does not.
     */
    private async inMeetingSignal(page: Page): Promise<string | null> {
        return page
            .evaluate(() => {
                // Still on the pre-join screen? Then definitely not in-meeting.
                const onPrejoin = document.querySelector('[data-tid="prejoin-join-button"]');
                if (onPrejoin) {
                    const r = (onPrejoin as HTMLElement).getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return null;
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
                return inMeetingSignals.find(visible) ?? null;
            })
            .catch(() => null);
    }

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        // Forward the speaker/attendee observers' in-page `DEBUG:` logs (and the
        // speakerChange signal) to CloudWatch — they otherwise go nowhere, and
        // they're useful for diagnosing in-meeting attribution on the anon client.
        page.on('console', (msg) => {
            const t = msg.text();
            if (t.startsWith('DEBUG:') || t.includes('speakerChange')) {
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
            await gotoMeetingPage(
                page,
                `https://teams.microsoft.com/v2/?meetingjoin=true#/meet/${details.invite.meetingId}?p=${details.invite.meetingPassword}&anon=true`,
                MEETING_HOST_PATTERNS.teams,
                'teams-join',
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
        const displayName = sanitizeTeamsDisplayName(details.scribeIdentity);
        if (displayName !== details.scribeIdentity) {
            console.log(
                `Display name "${details.scribeIdentity}" contains characters Teams rejects — joining as "${displayName}".`,
            );
        }
        await nameRes.element.evaluate((el: Element) => {
            const i = el as HTMLInputElement;
            i.focus();
            i.value = '';
        });
        await humanType(page, nameRes.element, displayName);
        const typedName = await nameRes.element.evaluate(
            (el: Element) => (el as HTMLInputElement).value || '',
        );
        if (typedName !== displayName) {
            console.warn(
                `Display-name value mismatch (expected "${displayName}", got "${typedName}") — clearing and re-typing.`,
            );
            await nameRes.element.evaluate((el: Element) => {
                const i = el as HTMLInputElement;
                i.focus();
                i.value = '';
            });
            await humanType(page, nameRes.element, displayName);
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
        // Attendee monitoring / auto-leave watchdog.
        //
        // The VP should leave once everyone else has gone, but the Teams
        // roster badge (span[data-tid="toolbar-item-badge"]) is a fragile
        // single signal: Teams hides/re-renders it during content-share,
        // full-screen, and gallery/speaker re-layouts, and a MutationObserver
        // callback can observe a transient empty text node. The previous
        // implementation fired the graceful-leave path on the FIRST reading
        // that parsed to <=1, so one transient DOM misread ended a live
        // meeting mid-sentence (GitHub #317, #318).
        //
        // Fix — mirror the debounced participants-watchdog already proven on
        // Zoom (see zoom.ts):
        //   - poll on a fixed interval instead of reacting to every mutation;
        //   - require pollsBeforeEnd *consecutive* genuine "<=1" reads
        //     (~60s grace) before leaving, so transient re-renders are absorbed;
        //   - corroborate a "badge missing/empty" reading against the in-meeting
        //     chrome, and when that chrome is present treat it as evidence of a
        //     live meeting rather than as a strike — on Teams the badge
        //     legitimately disappears mid-meeting, and because nothing here moves
        //     the pointer, an auto-hidden toolbar stays hidden for the whole of a
        //     share (GitHub #660);
        //   - only an uncorroborated miss counts, on its own wider bound
        //     (pollsBeforeEndMissing) — see decideAttendeeAction.
        // Audio silence is deliberately NOT a leave signal (long silent
        // document reviews are real meetings).
        const watchdogConfig = resolveAttendeeWatchdogConfig();
        console.log(
            'Listening for attendee changes ' +
                `(poll ${watchdogConfig.pollMs}ms, alone x${watchdogConfig.pollsBeforeEnd}, ` +
                `badge-missing-and-not-in-meeting x${watchdogConfig.pollsBeforeEndMissing}).`,
        );
        const watchdogState: AttendeeWatchdogState = {
            consecutiveLonely: 0,
            consecutiveMissing: 0,
            suppressedMissing: 0,
            unreadableRun: 0,
        };
        // setInterval does not await an async callback, so a tick that outlasts
        // the cadence would overlap the next one and each overlapping tick would
        // increment the counters independently — making the wall-clock tolerance
        // shorter than the configured number of polls. Not reachable at the 20s
        // default, but three sequential page.evaluate round trips at the 1000ms
        // minimum on a loaded host is a different matter.
        let pollInFlight = false;
        const attendeeWatchdog = setInterval(async () => {
            if (page.isClosed()) {
                clearInterval(attendeeWatchdog);
                return;
            }
            if (pollInFlight) return;
            pollInFlight = true;
            try {

                // Strongest signal first: Teams renders an unmistakable post-meeting
                // screen ("You left the meeting" / "The meeting has ended", or a
                // /postmeeting-style URL). Checking this before the roster badge means
                // a genuinely-ended meeting is detected in ONE poll instead of waiting
                // out the badge debounce (GitHub #540).
                let ended: { ended: boolean; how?: string } = { ended: false };
                try {
                    ended = await page.evaluate(() => {
                        const url = window.location.href;
                        if (/\/postmeeting|\/meetingended|calling\/end/i.test(url)) {
                            return { ended: true, how: `url:${url.slice(0, 120)}` };
                        }
                        const text = (document.body?.innerText || '').slice(0, 4000);
                        const phrases = [
                            /you (?:have )?left the meeting/i,
                            /the meeting (?:has )?ended/i,
                            /this meeting has ended/i,
                            /you(?:'|’)?ve been removed from (?:the|this) meeting/i,
                            /the call (?:has )?ended/i,
                        ];
                        for (const re of phrases) {
                            const m = text.match(re);
                            if (m) return { ended: true, how: `text:${m[0]}` };
                        }
                        return { ended: false };
                    });
                } catch {
                    // Page closed/navigated or CDP error — fall through to the badge
                    // reading, which handles unknown state with its own debounce.
                    ended = { ended: false };
                }

                if (ended.ended) {
                    console.log(`Teams meeting has ended (${ended.how}) — leaving.`);
                    clearInterval(attendeeWatchdog);
                    details.start = false;
                    this.requestEnd({ reason: 'host-ended', trigger: 'post-meeting-screen' });
                    return;
                }

                let reading: AttendeeReading;
                try {
                    reading = await page.evaluate(() => {
                        const badgeElement = document.querySelector('span[data-tid="toolbar-item-badge"]');
                        if (!badgeElement) return { state: 'BADGE_MISSING' as const };
                        const text = (badgeElement.textContent || '').trim();
                        if (text === '') return { state: 'BADGE_MISSING' as const };
                        const n = parseInt(text, 10);
                        return { state: 'OK' as const, count: Number.isFinite(n) ? n : 0 };
                    });
                } catch {
                    // page.evaluate threw — page closed/navigated or CDP error.
                    reading = { state: 'BADGE_MISSING' };
                }

                // An unreadable badge on its own says nothing: it is equally the
                // signature of a meeting that ended and of a toolbar that collapsed
                // during a share. isInMeeting() tests ~18 in-meeting signals — the
                // stage, the calling screen, the call controls — so it distinguishes
                // the two without depending on the toolbar being expanded or on the
                // client's UI language. Only a miss with NO in-meeting chrome behind
                // it is allowed to count toward leaving.
                let inMeetingVia: string | null = null;
                if (reading.state === 'BADGE_MISSING') {
                    inMeetingVia = await this.inMeetingSignal(page);
                    if (inMeetingVia !== null) reading = { state: 'BADGE_MISSING_IN_MEETING' };
                }

                const decision = decideAttendeeAction(reading, watchdogState, watchdogConfig);
                if (reading.state === 'OK') {
                    console.log(
                        `DEBUG: Teams attendee count: ${reading.count}, ` +
                            `hasOthers: ${reading.count > 1}, ` +
                            `consecutiveLonely: ${watchdogState.consecutiveLonely}/${watchdogConfig.pollsBeforeEnd}`,
                    );
                } else if (reading.state === 'BADGE_MISSING_IN_MEETING') {
                    const spent = watchdogState.suppressedMissing > watchdogConfig.maxSuppressedPolls;
                    console.log(
                        'DEBUG: Teams attendee badge missing/empty but in-meeting chrome is present ' +
                            `(matched ${inMeetingVia}) — ` +
                            `${watchdogState.suppressedMissing}/${watchdogConfig.maxSuppressedPolls} ` +
                            (spent
                                ? 'vetoes used, allowance SPENT: counting this as a miss from now on'
                                : 'vetoes used, not counting toward leaving. Expected during a ' +
                                  'content share or with a collapsed toolbar.'),
                    );
                } else {
                    console.log(
                        'DEBUG: Teams attendee badge missing/empty AND no in-meeting chrome — ' +
                            `${watchdogState.consecutiveMissing}/${watchdogConfig.pollsBeforeEndMissing} consecutive ` +
                            '(the shape of a meeting that has ended; the post-meeting screen, when Teams ' +
                            'shows one, ends the meeting in a single poll instead)',
                    );
                }

                if (decision.action === 'end') {
                    console.log(`LMA Virtual Participant is leaving: ${decision.detail}`);
                    clearInterval(attendeeWatchdog);
                    details.start = false;
                    this.requestEnd({ reason: decision.reason, trigger: decision.trigger });
                }
            } finally {
                pollInFlight = false;
            }
        }, watchdogConfig.pollMs);
        page.once('close', () => clearInterval(attendeeWatchdog));

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

        await new Promise((resolve) => setTimeout(resolve, 1000));
        console.log("Listening for message changes.");
        // Incoming-message reader. The chat list is a VIRTUALIZED Fluent UI list
        // (message-pane-list-runway/viewport), so rows are recycled rather than
        // cleanly "added" — a childList MutationObserver misses them, and the
        // exact v2 node shape (div[dir=auto][role=heading][aria-level=4]) does
        // not exist on the anon light-meetings client. So instead of observing
        // mutations we POLL the rendered chat rows every ~2s and diff a seen-set.
        // The AI selector resolver is a one-shot request/response and the wrong
        // tool to run per-message, so it's NOT used per poll; instead it's a
        // bounded fallback (below) that fires once if our heuristic extraction
        // comes up empty for a sustained window, to survive a future redesign.
        await page.evaluate(() => {
            (window as any).__lmaSeenMessages = new Set<string>();
            // Extraction driven by the real anon DOM (confirmed via dump): each
            // message renders an author span [data-tid="message-author-name"]
            // and a body in a [role="heading"] / [id^="content-"] node. Scrape
            // every message-ish row and return any not-yet-seen text. Also
            // accepts an optional extra selector discovered by the AI fallback.
            (window as any).__lmaScrapeMessages = (extraSelector?: string): string[] => {
                const seen: Set<string> = (window as any).__lmaSeenMessages;
                const out: string[] = [];
                const MESSAGE_SELECTORS = [
                    'div[dir="auto"][role="heading"][aria-level="4"]', // v2 client
                    '[data-tid="messageBodyContent"]',
                    '[data-tid="messageContent"]',
                    '[id^="content-message-body-"]',
                    '[id^="content-"][class*="Primitive"]',
                    '[data-tid="control-message-renderer"]',
                    '[role="heading"]',
                    '.ui-chat__messagecontent',
                ];
                if (extraSelector) MESSAGE_SELECTORS.unshift(extraSelector);
                const nodes = new Set<Element>();
                for (const sel of MESSAGE_SELECTORS) {
                    try {
                        document.querySelectorAll(sel).forEach((n) => nodes.add(n));
                    } catch {
                        /* bad selector from AI — ignore */
                    }
                }
                for (const node of nodes) {
                    const t = (node.textContent || '').replace(/\s+/g, ' ').trim();
                    // Skip empties and the system "invited to the meeting" notices.
                    if (!t || t.length > 2000) continue;
                    if (/was invited to the meeting/i.test(t)) continue;
                    if (seen.has(t)) continue;
                    seen.add(t);
                    out.push(t);
                }
                return out;
            };
        });

        // One-shot diagnostic dump of the chat-list DOM (confirms real selectors
        // in CloudWatch). Kept — cheap and behind a single call.
        await this.dumpChatListDom(page);

        // Node-side poll loop. Runs until the page closes; each tick scrapes new
        // messages and routes them through messageChange (which handles end /
        // pause / resume / transcript-capture). emptyStreak drives the bounded
        // AI fallback: if we keep finding the chat list but extract nothing for
        // ~30s while it clearly has content, ask the AI resolver ONCE for the
        // message-node selector and feed it into the scraper.
        let aiMessageSelector: string | undefined;
        let aiTried = false;
        let emptyStreak = 0;
        const pollMessages = async (): Promise<void> => {
            if (page.isClosed()) return;
            try {
                const fresh: string[] = await page.evaluate(
                    (extra) => (window as any).__lmaScrapeMessages(extra),
                    aiMessageSelector,
                );
                if (fresh.length) {
                    emptyStreak = 0;
                    for (const text of fresh) {
                        await this.handleIncomingMessage(page, text);
                    }
                } else if (!aiTried) {
                    // Nothing extracted this tick. If the chat list visibly has
                    // content but our selectors find nothing for ~15 ticks (~30s),
                    // try the AI resolver ONCE to discover the message-body node.
                    emptyStreak += 1;
                    if (emptyStreak >= 15 && isResolverEnabled()) {
                        aiTried = true;
                        try {
                            const res = await findElementWithFallback(
                                page,
                                [],
                                {
                                    intent: 'A single Microsoft Teams chat message body element in the in-meeting chat panel (the text a participant typed, e.g. a "LMA leave" command). NOT the compose box, NOT a system "was invited" notice.',
                                    platform: 'TEAMS',
                                    step: 'teams.chat.messageNode',
                                    useScreenshot: true,
                                },
                                { maxRetries: 1, delayMs: 200 },
                            );
                            if (res?.selector) {
                                aiMessageSelector = res.selector;
                                console.log(`[teams] AI discovered chat message selector: ${res.selector}`);
                            }
                        } catch (e) {
                            console.warn('[teams] AI message-selector discovery failed (non-fatal):', e);
                        }
                    }
                }
            } catch {
                /* page navigating / context gone — try again next tick */
            }
            if (!page.isClosed()) setTimeout(() => { void pollMessages(); }, 2000);
        };
        void pollMessages();
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
