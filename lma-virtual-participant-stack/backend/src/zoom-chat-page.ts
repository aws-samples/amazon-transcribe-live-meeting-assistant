/**
 * Reading the Zoom browser join's meeting chat, and keeping its panel open.
 *
 * Zoom's web client draws the chat panel only while it is open, and it closes
 * the panel by itself: when a participant starts sharing their screen the
 * meeting switches to the share layout and the side panel goes. The rows are
 * then unmounted, and they are drawn again (new elements) when the panel is
 * reopened. So the chat is read by polling (readZoomChatInPage), not by an
 * observer fixed to one list element, and the panel is reopened when the
 * toolbar shows it closed (ZoomChatPanelKeeper).
 *
 * Each message row is `div[id^="chat-message-content-"][role="row"]` whose
 * aria-label is "<sender> to <recipient>, <time>, <text>" (see
 * parseZoomChatLabel in zoom.ts), inside `[id^="chat-item-container-"]` whose
 * `data-id` is Zoom's message id. The row's own id carries the row's position
 * in the list, not the message id, so the message id is preferred as the key.
 *
 * Nothing here imports Playwright: the in-page functions are passed whole to
 * page.evaluate, and the rest is plain logic, so both are unit-tested.
 */

/** Selectors the in-page functions use, most specific first. */
export const ZOOM_CHAT_SELECTORS = {
    /** The chat message list. The first that matches is read. */
    lists: ['div[aria-label="Chat Message List"]', '.chat-container__chat-list'],
    /**
     * A message row, tried in order inside the list; the first that finds any
     * row is used. The last is structural: a labelled grid row.
     */
    rows: ['[id^="chat-message-content-"][aria-label]', '[role="row"][aria-label]'],
    /** The element around a row that carries Zoom's message id in `data-id`. */
    container: '[id^="chat-item-container-"]',
    /** The toolbar button while the panel is closed ("open the chat panel", then " 2 unread messages" when there are). */
    openButton: 'button[aria-label^="open the chat panel" i]',
    /** The same button while the panel is open. */
    closeButton: 'button[aria-label^="close the chat panel" i]',
} as const;

export type ZoomChatSelectors = {
    lists: readonly string[];
    rows: readonly string[];
    container: string;
};

/** One chat row as read from the page. */
export interface ZoomChatRaw {
    /** The row's aria-label, sender and recipient included. */
    label: string;
    /** Zoom's message id when the row carries one, else the row element's id suffix, else null. */
    id: string | null;
    links: string[];
}

export interface ZoomChatReading {
    /** False when no chat list is on the page (the panel is closed). */
    listFound: boolean;
    /** True while the rows found are taken as already there when the VP started listening. */
    baseline: boolean;
    /** The rows not read before, in list order, the VP's own excluded. */
    rows: ZoomChatRaw[];
}

/**
 * Reads the chat rows not read before, in the page (passed whole to
 * page.evaluate, so it must not use anything outside its own body).
 *
 * The first read that finds the list, and any within `settleMs` of it, are the
 * baseline: the rows then on screen were there before the VP was listening
 * (its own introduction, and on a meeting with a Team Chat group chat the
 * group's earlier messages), so they are marked read and not returned. After
 * that, a row not read before is returned once, however often Zoom draws it
 * again: when the panel is closed and reopened, the rows already read are
 * skipped and the ones that arrived while it was closed are returned.
 *
 * A row is keyed on Zoom's message id, so the same command typed twice is read
 * twice. A row without one is keyed on its element id and its label together,
 * so a row reusing a position is not taken for the message that held it.
 * The VP's own messages ("You to ...") are marked read and not returned.
 */
export function readZoomChatInPage(sels: ZoomChatSelectors & { settleMs: number }): ZoomChatReading {
    const w = window as any;
    const state: { seen: Set<string>; listFirstSeenAt: number | null } = (w.__lmaZoomChat ||= {
        seen: new Set<string>(),
        listFirstSeenAt: null,
    });
    let list: Element | null = null;
    for (const s of sels.lists) {
        list = document.querySelector(s);
        if (list) break;
    }
    if (!list) return { listFound: false, baseline: state.listFirstSeenAt === null, rows: [] };
    const now = Date.now();
    const firstRead = state.listFirstSeenAt === null;
    if (firstRead) state.listFirstSeenAt = now;
    const baseline = firstRead || now - (state.listFirstSeenAt as number) < sels.settleMs;
    let rows: Element[] = [];
    for (const s of sels.rows) {
        rows = Array.from(list.querySelectorAll(s));
        if (rows.length) break;
    }
    const out: ZoomChatRaw[] = [];
    for (const row of rows) {
        const label = (row.getAttribute('aria-label') || '').trim();
        // No label yet: the row is filled in by a later render, so it is not
        // marked read until it has one.
        if (!label) continue;
        const container = row.closest(sels.container);
        const messageId = container?.getAttribute('data-id') || null;
        const rowId = row.id || '';
        const key = messageId ? `id:${messageId}` : `row:${rowId}|${label}`;
        if (state.seen.has(key)) continue;
        state.seen.add(key);
        if (baseline) continue;
        if (/^you to /i.test(label)) continue;
        const scope = container || row;
        const links = Array.from(scope.querySelectorAll('a[href]'))
            .map((a) => (a as HTMLAnchorElement).href)
            .filter(Boolean);
        const id = messageId || (rowId ? rowId.replace(/^chat-message-content-?/, '') || rowId : null);
        out.push({ label, id, links });
    }
    return { listFound: true, baseline, rows: out };
}

/** Whether the toolbar shows the chat panel open, closed, or neither (the toolbar is hidden or renamed). */
export type ZoomChatPanelState = 'open' | 'closed' | 'unknown';

/** Reads the chat panel's state from the toolbar button, in the page (self-contained, as above). */
export function zoomChatPanelStateInPage(sels: { openButton: string; closeButton: string }): ZoomChatPanelState {
    if (document.querySelector(sels.closeButton)) return 'open';
    if (document.querySelector(sels.openButton)) return 'closed';
    return 'unknown';
}

/**
 * When to reopen the chat panel. Only a toolbar showing the panel closed
 * reopens it: the button is a toggle, so the keeper never clicks it when the
 * panel may be open, and never asks the AI resolver for it (a resolved
 * selector could be the button in its "close" state). Attempts are spaced by
 * `minGapMs`, doubled after each one that leaves the panel closed, up to
 * `maxGapMs`, and back to `minGapMs` once the panel is seen open.
 */
export class ZoomChatPanelKeeper {
    private lastAttemptAt = Number.NEGATIVE_INFINITY;
    private gapMs: number;

    constructor(
        private readonly minGapMs = 5_000,
        private readonly maxGapMs = 120_000,
    ) {
        this.gapMs = minGapMs;
    }

    /** True when the panel should be reopened now. Call once per poll with the state read from the page. */
    shouldReopen(state: ZoomChatPanelState, now: number): boolean {
        if (state !== 'closed') {
            if (state === 'open') this.gapMs = this.minGapMs;
            return false;
        }
        return now - this.lastAttemptAt >= this.gapMs;
    }

    /** Records an attempt and whether the panel was open after it. */
    attempted(now: number, opened: boolean): void {
        this.lastAttemptAt = now;
        this.gapMs = opened ? this.minGapMs : Math.min(this.gapMs * 2, this.maxGapMs);
    }

    /** The current spacing between attempts (for the log line). */
    get currentGapMs(): number {
        return this.gapMs;
    }
}
