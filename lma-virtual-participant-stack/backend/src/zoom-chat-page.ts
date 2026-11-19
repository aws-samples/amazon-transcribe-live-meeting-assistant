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
 * A row's attachments (chat-attachments.ts) are read with it. Measured with
 * the VP signed in to Zoom (web client, November 2026): a pasted image is an
 * `img.chat-image-preview-img` whose `src` is a `blob:` address, its file name
 * in `alt`; a shared file is a `div.chat-file-item` card with its name in
 * `.chat-file-item__name` (and the card's aria-label), its size in
 * `.chat-file-item__size`, and no link: clicking it makes Zoom hand the file to
 * the browser as a download. Every row also holds Zoom's emoji reaction icons
 * as `<img>` elements, which is why only those two classes are read.
 *
 * A pasted image can finish uploading after its message's text is drawn: the
 * row is then read with no image. So a row is watched for `watchMs` after it is
 * first read, and an attachment that appears on it meanwhile is returned once,
 * in `late`, to be added to the chat line the row became. An image is not
 * taken until it has its `blob:` address; one still without it when the window
 * closes is returned then, with no address, so it is recorded by name.
 *
 * Nothing here imports Playwright: the in-page functions are passed whole to
 * page.evaluate, and the rest is plain logic, so both are unit-tested.
 */
import type { AttachmentFetchResult, IncomingChatAttachment } from './chat-attachments.js';

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
    /** A pasted image in a row; not the emoji reaction icons, which are images too. */
    imagePreview: 'img.chat-image-preview-img',
    /** A shared file's card in a row, and its name and size. */
    fileCard: '.chat-file-item',
    fileName: '.chat-file-item__name',
    fileSize: '.chat-file-item__size',
} as const;

export type ZoomChatSelectors = {
    lists: readonly string[];
    rows: readonly string[];
    container: string;
    /** The attachment selectors; a row's attachments are not read without them. */
    imagePreview?: string;
    /**
     * How long, in milliseconds, a row is watched after it is first read for
     * attachments that appear later (`ZoomChatReading.late`). 0 or absent: not
     * watched.
     */
    watchMs?: number;
    fileCard?: string;
    fileName?: string;
    fileSize?: string;
};

/** One attachment of a chat row, as read from the page. */
export interface ZoomChatAttachmentRaw {
    kind: 'image' | 'document';
    name: string;
    /** The image's address (a `blob:` one when Zoom has loaded it). */
    src?: string;
    /** The size Zoom shows on a file card ("204 KB"). */
    sizeText?: string | null;
    /** The file card's position among the row's cards, for clicking it. */
    index: number;
}

/** One chat row as read from the page. */
export interface ZoomChatRaw {
    /** The row's aria-label, sender and recipient included. */
    label: string;
    /** Zoom's message id when the row carries one, else the row element's id suffix, else null. */
    id: string | null;
    links: string[];
    /** Zoom's message id (the container's `data-id`) alone, or null; a file card is clicked through it. */
    messageId?: string | null;
    /** The row's images and file cards. */
    attachments?: ZoomChatAttachmentRaw[];
}

export interface ZoomChatReading {
    /** False when no chat list is on the page (the panel is closed). */
    listFound: boolean;
    /** True while the rows found are taken as already there when the VP started listening. */
    baseline: boolean;
    /** The rows not read before, in list order, the VP's own excluded. */
    rows: ZoomChatRaw[];
    /**
     * Rows read before, within `watchMs` of their first reading, that hold
     * attachments not seen on them before: those attachments alone, each
     * returned once.
     */
    late?: ZoomChatRaw[];
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
    const state: {
        seen: Set<string>;
        listFirstSeenAt: number | null;
        watched?: Map<string, { at: number; attachments: Set<string>; row: { label: string; id: string | null; messageId: string | null }; unloaded: Map<number, string> }>;
    } = (w.__lmaZoomChat ||= {
        seen: new Set<string>(),
        listFirstSeenAt: null,
    });
    const watched = (state.watched ||= new Map<string, { at: number; attachments: Set<string>; row: { label: string; id: string | null; messageId: string | null }; unloaded: Map<number, string> }>());
    let list: Element | null = null;
    for (const s of sels.lists) {
        list = document.querySelector(s);
        if (list) break;
    }
    if (!list) return { listFound: false, baseline: state.listFirstSeenAt === null, rows: [], late: [] };
    const now = Date.now();
    const watchMs = sels.watchMs ?? 0;
    const late: ZoomChatRaw[] = [];
    // A row whose window has closed: an image it still shows without a blob:
    // address never loaded, and is returned by name, with no address.
    for (const [key, entry] of watched) {
        if (now - entry.at < watchMs) continue;
        watched.delete(key);
        if (entry.unloaded.size) {
            late.push({
                ...entry.row,
                links: [],
                attachments: Array.from(entry.unloaded, ([index, name]) => ({ kind: 'image' as const, name, src: '', index })),
            });
        }
    }
    const firstRead = state.listFirstSeenAt === null;
    if (firstRead) state.listFirstSeenAt = now;
    const baseline = firstRead || now - (state.listFirstSeenAt as number) < sels.settleMs;
    let rows: Element[] = [];
    for (const s of sels.rows) {
        rows = Array.from(list.querySelectorAll(s));
        if (rows.length) break;
    }
    // The images of a row that do not have their blob: address yet, by position.
    const unloadedIn = (scope: Element): Map<number, string> => {
        const found = new Map<number, string>();
        if (!sels.imagePreview) return found;
        Array.from(scope.querySelectorAll(sels.imagePreview)).forEach((img, index) => {
            const src = (img as HTMLImageElement).currentSrc || img.getAttribute('src') || '';
            if (!/^blob:/i.test(src)) found.set(index, (img.getAttribute('alt') || '').trim());
        });
        return found;
    };
    // A row's attachments as they are now. An image is taken only once Zoom has
    // given it its blob: address: until then it is still being uploaded.
    const attachmentsIn = (scope: Element): ZoomChatAttachmentRaw[] => {
        const found: ZoomChatAttachmentRaw[] = [];
        if (sels.imagePreview) {
            Array.from(scope.querySelectorAll(sels.imagePreview)).forEach((img, index) => {
                const src = (img as HTMLImageElement).currentSrc || img.getAttribute('src') || '';
                if (!/^blob:/i.test(src)) return;
                found.push({ kind: 'image', name: (img.getAttribute('alt') || '').trim(), src, index });
            });
        }
        if (sels.fileCard) {
            Array.from(scope.querySelectorAll(sels.fileCard)).forEach((card, index) => {
                const named = sels.fileName ? card.querySelector(sels.fileName) : null;
                const sized = sels.fileSize ? card.querySelector(sels.fileSize) : null;
                const name = ((named?.textContent || '').trim() || (card.getAttribute('aria-label') || '').trim()).replace(/\s+/g, ' ');
                found.push({ kind: 'document', name, sizeText: (sized?.textContent || '').trim() || null, index });
            });
        }
        return found;
    };
    const attachmentKey = (a: ZoomChatAttachmentRaw): string => (a.kind === 'image' ? `img:${a.src}` : `file:${a.index}:${a.name}`);
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
        const scope = container || row;
        const id = messageId || (rowId ? rowId.replace(/^chat-message-content-?/, '') || rowId : null);
        if (state.seen.has(key)) {
            // Read before, and still watched: an attachment that has appeared
            // since (a pasted image finishing its upload after the text).
            const entry = watched.get(key);
            if (!entry) continue;
            entry.unloaded = unloadedIn(scope);
            const added = attachmentsIn(scope).filter((a) => !entry.attachments.has(attachmentKey(a)));
            if (!added.length) continue;
            added.forEach((a) => entry.attachments.add(attachmentKey(a)));
            late.push({ label, id, links: [], messageId, attachments: added });
            continue;
        }
        state.seen.add(key);
        if (baseline) continue;
        if (/^you to /i.test(label)) continue;
        const links = Array.from(scope.querySelectorAll('a[href]'))
            .map((a) => (a as HTMLAnchorElement).href)
            .filter(Boolean);
        const attachments = attachmentsIn(scope);
        if (watchMs > 0) {
            watched.set(key, {
                at: now,
                attachments: new Set(attachments.map(attachmentKey)),
                row: { label, id, messageId },
                unloaded: unloadedIn(scope),
            });
        }
        out.push({ label, id, links, messageId, ...(attachments.length ? { attachments } : {}) });
    }
    return { listFound: true, baseline, rows: out, late };
}

/** How a Zoom row's attachments are fetched; zoom.ts gives the page-bound halves. */
export interface ZoomAttachmentFetchers {
    /** For an image with a `blob:` address. */
    blob: (src: string) => (maxBytes: number) => Promise<AttachmentFetchResult>;
    /** For the `index`-th file card of the message whose container has `data-id` `messageId`. */
    fileCard: (messageId: string, index: number) => (maxBytes: number) => Promise<AttachmentFetchResult>;
}

/** A message id that can be put in an attribute selector as it is. */
const SELECTOR_SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/;

/**
 * The attachments of a row, for the chat capture: an image with a `blob:`
 * address is fetched in the page, and one without is recorded as unavailable;
 * a file card is fetched by clicking it, only when the row has a message id
 * that names its container exactly, and is otherwise offered by name alone.
 */
export function zoomChatAttachments(row: ZoomChatRaw, fetchers: ZoomAttachmentFetchers): IncomingChatAttachment[] {
    return (row.attachments ?? []).map((raw) => {
        if (raw.kind === 'image') {
            const src = raw.src ?? '';
            // An image Zoom has not loaded yet has no blob: address: it could
            // not be read, rather than not be downloadable.
            const unavailable = async (): Promise<AttachmentFetchResult> => ({ ok: false, reason: 'unavailable' });
            return { kind: 'image', name: raw.name, fetch: /^blob:/i.test(src) ? fetchers.blob(src) : unavailable };
        }
        const messageId = row.messageId ?? '';
        return {
            kind: 'document',
            name: raw.name,
            size: parseShownSize(raw.sizeText),
            ...(SELECTOR_SAFE_ID.test(messageId) ? { fetch: fetchers.fileCard(messageId, raw.index) } : {}),
        };
    });
}

const SIZE_UNITS: Record<string, number> = { b: 1, bytes: 1, kb: 1024, mb: 1024 ** 2, gb: 1024 ** 3 };

/** The bytes of a size as Zoom shows it on a file card ("204 KB", "1.5 MB"), or null. */
export function parseShownSize(text: string | null | undefined): number | null {
    const match = /^\s*([\d.,]+)\s*(b|bytes|kb|mb|gb)\s*$/i.exec(text ?? '');
    if (!match) return null;
    const value = Number(match[1].replace(/,/g, ''));
    if (!Number.isFinite(value)) return null;
    return Math.round(value * SIZE_UNITS[match[2].toLowerCase()]);
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
