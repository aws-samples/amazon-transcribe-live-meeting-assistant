/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading chat attachments from the meeting page (see chat-attachments.ts).
 *
 * - `fetchBlobInPage` runs in the page: it fetches an image's `blob:` address,
 *   which only the page can read, and returns its bytes as base64, refusing
 *   anything over the cap. Measured on Zoom (signed in) and the Teams
 *   light-meetings client (anonymous guest): the fetch returns the original
 *   image.
 * - `readTeamsChatAttachmentsInPage` runs in the page: the attachments of the
 *   Teams chat's messages not read before. A pasted image is an `<img>` with a
 *   `blob:` address in the message's file-attachment grid, its file name in
 *   `alt`. A shared document is only a file card (`data-tid="file-chiclet-<file
 *   name>"`) whose content a guest cannot download, so it is returned by name.
 * - `pageBlobFetcher` and `downloadFetcher` are the node-side halves the
 *   platform handlers give each attachment as its `fetch`.
 *
 * The Zoom rows' attachments are read with the rest of the row, in
 * readZoomChatInPage (zoom-chat-page.ts).
 *
 * Nothing here imports Playwright: the in-page functions are passed whole to
 * page.evaluate, and the page and the download are typed by what is used of
 * them, so all of it is unit-tested.
 */
import { readFile, stat } from 'node:fs/promises';
import type { AttachmentFetchResult, IncomingChatAttachment } from './chat-attachments.js';
import type { IncomingChat } from './meeting-chat.js';

/** What `fetchBlobInPage` returns. */
export type PageBlobResult =
    | { ok: true; base64: string; size: number }
    | { ok: false; reason: 'too-large' | 'unavailable'; size?: number };

/**
 * Fetches a `blob:` address in the page and returns its bytes as base64 (passed
 * whole to page.evaluate, so it must not use anything outside its own body).
 * Any other address is refused: the attachments measured are all `blob:`, and
 * the VP's browser fetches nothing else on a reader's behalf.
 */
export async function fetchBlobInPage(args: { src: string; maxBytes: number }): Promise<PageBlobResult> {
    try {
        if (typeof args.src !== 'string' || !/^blob:/i.test(args.src)) return { ok: false, reason: 'unavailable' };
        const response = await fetch(args.src);
        if (!response.ok) return { ok: false, reason: 'unavailable' };
        const blob = await response.blob();
        if (blob.size > args.maxBytes) return { ok: false, reason: 'too-large', size: blob.size };
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let binary = '';
        const CHUNK = 0x8000;
        for (let at = 0; at < bytes.length; at += CHUNK) {
            binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(at, at + CHUNK)));
        }
        return { ok: true, base64: btoa(binary), size: bytes.length };
    } catch {
        return { ok: false, reason: 'unavailable' };
    }
}

/** What is used of a Playwright page to fetch a `blob:` address in it. */
export interface EvaluatingPage {
    evaluate<R, A>(fn: (arg: A) => R | Promise<R>, arg: A): Promise<R>;
}

/** An attachment's `fetch`, for an image whose `blob:` address the page can read. */
export function pageBlobFetcher(page: EvaluatingPage, src: string): (maxBytes: number) => Promise<AttachmentFetchResult> {
    return async (maxBytes) => {
        const result = await page.evaluate(fetchBlobInPage, { src, maxBytes }).catch((): PageBlobResult => ({ ok: false, reason: 'unavailable' }));
        if (!result.ok) return result;
        return { ok: true, bytes: Buffer.from(result.base64, 'base64') };
    };
}

/** What is used of a Playwright download. */
export interface FinishedDownload {
    failure(): Promise<string | null>;
    path(): Promise<string | null>;
    delete(): Promise<void>;
}

/**
 * Reads a download the browser saved, refusing anything over `maxBytes`, and
 * deletes the browser's copy either way. The file is only read: never opened
 * with anything, never run.
 */
export async function readDownload(download: FinishedDownload, maxBytes: number): Promise<AttachmentFetchResult> {
    try {
        if (await download.failure()) return { ok: false, reason: 'unavailable' };
        const file = await download.path();
        if (!file) return { ok: false, reason: 'unavailable' };
        const { size } = await stat(file);
        if (size > maxBytes) return { ok: false, reason: 'too-large', size };
        return { ok: true, bytes: await readFile(file) };
    } catch {
        return { ok: false, reason: 'unavailable' };
    } finally {
        await download.delete().catch(() => undefined);
    }
}

/**
 * An attachment's `fetch`, for a file the platform serves as a browser
 * download when its card is clicked: `start` clicks the card and resolves to
 * the download (Zoom; see zoom.ts).
 */
export function downloadFetcher(start: () => Promise<FinishedDownload>): (maxBytes: number) => Promise<AttachmentFetchResult> {
    return async (maxBytes) => {
        let download: FinishedDownload;
        try {
            download = await start();
        } catch {
            return { ok: false, reason: 'unavailable' };
        }
        return readDownload(download, maxBytes);
    };
}

// ─── Teams ───────────────────────────────────────────────────────────────────

/** Selectors the Teams reader uses. */
export const TEAMS_ATTACHMENT_SELECTORS = {
    /** The element around a message's pasted images. */
    grids: ['[data-tid*="file-attachment-grid"]', '[class*="file-attachment-grid"]'],
    /** A shared file's card; its data-tid is "file-chiclet-<file name>". */
    fileCard: '[data-tid^="file-chiclet-"]',
    /** A message row, as the chat reader in teams.ts finds them. */
    row: '[data-mid], [data-tid="chat-pane-message"], [data-tid="chat-pane-item"], [role="listitem"]',
    author: '[data-tid="message-author-name"]',
} as const;

export type TeamsAttachmentSelectors = {
    grids: readonly string[];
    fileCard: string;
    row: string;
    author: string;
};

/** One attachment as read from the Teams page. */
export interface TeamsAttachmentRaw {
    kind: 'image' | 'document';
    name: string;
    /** The image's `blob:` address; absent for a file card. */
    src?: string;
}

/** The attachments of one Teams message. */
export interface TeamsAttachmentRow {
    /** The message's id (the row's data-mid), when it has one. */
    id: string | null;
    author: string | null;
    time: string | null;
    /** What tells this message's attachments apart when it has no id. */
    keys: string[];
    attachments: TeamsAttachmentRaw[];
}

/**
 * The attachments of the Teams chat's messages not read before, one entry per
 * message, in page order (passed whole to page.evaluate, so it must not use
 * anything outside its own body). An image is keyed on its `blob:` address and
 * a file card on its message and name, so an attachment Teams draws again, or
 * draws in both copies of a message (the light-meetings client renders each
 * message twice), is returned once. An image whose address is not a `blob:`
 * one yet is not marked read, so it is returned once Teams has loaded it.
 */
export function readTeamsChatAttachmentsInPage(sels: TeamsAttachmentSelectors): TeamsAttachmentRow[] {
    const w = window as any;
    const seen: Set<string> = (w.__lmaTeamsAttachmentsSeen ||= new Set<string>());
    const byRow = new Map<Element | null, TeamsAttachmentRow>();
    const order: Array<Element | null> = [];
    const entryFor = (row: Element | null): TeamsAttachmentRow => {
        let entry = byRow.get(row);
        if (!entry) {
            const author = (row?.querySelector(sels.author)?.textContent || '').replace(/\s+/g, ' ').trim() || null;
            entry = {
                id: row?.getAttribute('data-mid') || null,
                author,
                time: row?.querySelector('time')?.getAttribute('datetime') || null,
                keys: [],
                attachments: [],
            };
            byRow.set(row, entry);
            order.push(row);
        }
        return entry;
    };
    const grids = new Set<Element>();
    for (const s of sels.grids) {
        try {
            document.querySelectorAll(s).forEach((g) => grids.add(g));
        } catch {
            /* a selector this page does not support */
        }
    }
    grids.forEach((grid) => {
        grid.querySelectorAll('img').forEach((img) => {
            const src = (img as HTMLImageElement).currentSrc || img.getAttribute('src') || '';
            if (!/^blob:/i.test(src)) return;
            const key = `img:${src}`;
            if (seen.has(key)) return;
            seen.add(key);
            const entry = entryFor(grid.closest(sels.row));
            entry.keys.push(key);
            entry.attachments.push({ kind: 'image', name: (img.getAttribute('alt') || '').trim(), src });
        });
    });
    document.querySelectorAll(sels.fileCard).forEach((card) => {
        const name = (card.getAttribute('data-tid') || '').replace(/^file-chiclet-/, '').trim();
        if (!name) return;
        const row = card.closest(sels.row);
        const key = `file:${row?.getAttribute('data-mid') || ''}|${name}`;
        if (seen.has(key)) return;
        seen.add(key);
        const entry = entryFor(row);
        entry.keys.push(key);
        entry.attachments.push({ kind: 'document', name });
    });
    return order.map((row) => byRow.get(row) as TeamsAttachmentRow);
}

/**
 * The chat messages to offer for the attachments read from the Teams page:
 * one per message, with no text (its own text, when it has any, is read by the
 * chat reader as before and added as its own line), the message's author, and
 * an id of its own so it is never taken for the text. An image with a `blob:`
 * address is fetched in the page; a file card is offered by name alone, since
 * a guest cannot download it.
 */
export function teamsAttachmentChats(
    rows: readonly TeamsAttachmentRow[],
    history: boolean,
    blob: (src: string) => (maxBytes: number) => Promise<AttachmentFetchResult>,
): IncomingChat[] {
    return rows
        .filter((row) => row.attachments.length)
        .map((row) => ({
            platform: 'teams',
            text: '',
            sender: row.author,
            id: row.id ? `${row.id}-attachments` : `attachments-${row.keys.join('|')}`,
            platformTime: row.time,
            // A Teams meeting chat is one thread everyone in the meeting reads.
            audience: 'everyone',
            history,
            attachments: row.attachments.map(
                (raw): IncomingChatAttachment => ({
                    kind: raw.kind,
                    name: raw.name,
                    ...(raw.kind === 'image' && raw.src && /^blob:/i.test(raw.src) ? { fetch: blob(raw.src) } : {}),
                }),
            ),
        }));
}
