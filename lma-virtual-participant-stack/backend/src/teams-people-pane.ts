/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading the participant list from the People pane on the Teams browser join
 * (teams.ts), on the anonymous light-meetings client.
 *
 * The People pane shares the side rail with the chat pane. In live meetings on
 * 2026-11-15 the chat compose box stayed on screen while the People pane was
 * open and after it was closed, and posting kept working, over about 14
 * openings. Earlier live VPs saw opening the People pane replace the chat for
 * the rest of the meeting. So every reading is followed by a check that the
 * compose box is still there (peopleCloseVerdict), one attempt to bring it
 * back when it is not, and, when that fails, the People pane is not opened
 * again in that meeting (PeoplePaneGuard).
 *
 * The pane's DOM, as measured in those meetings: the list is
 * `[role="tree"][data-tid="calling-roster-attendees"]`; a section header is a
 * treeitem with aria-level 1 ("In this meeting (3)"); each participant is a
 * treeitem with aria-level 2 holding a `span[title]` with the display name, and
 * an optional role badge (`[data-tid="ts-roster-organizer-status"]`,
 * "Organizer"). The VP itself is listed. The page script in teams.ts reads
 * that into PeopleTreeRow values; parsePeopleTree, here, makes the list.
 */

import { normalizeParticipantName } from './consent-matcher.js';

/** The People button in the meeting toolbar, in the order they are tried. */
export const TEAMS_PEOPLE_BUTTON_SELECTORS = ['#roster-button', '[data-tid="roster-button"]', 'button[aria-label*="People" i]'];

/** The participant list inside the People pane. */
export const TEAMS_PEOPLE_TREE_SELECTOR = '[role="tree"][data-tid="calling-roster-attendees"]';

/** A participant's role badge ("Organizer"), which is not part of the name. */
export const TEAMS_PEOPLE_ROLE_BADGE_SELECTOR = '[data-tid="ts-roster-organizer-status"]';

/** How long the People pane is given to show its list after the People button is clicked. */
export const PEOPLE_TREE_WAIT_MS = 3000;

/** The People pane is opened for the participant list at most this often. */
export const PEOPLE_READ_INTERVAL_MS = 60_000;

/** One treeitem of the People pane's list, as the page script reads it. */
export interface PeopleTreeRow {
    /** Its aria-level: 1 for a section header, 2 for a participant. */
    level: number;
    /** Its own text, without that of the treeitems nested in it. */
    text: string;
    /** The title of the first `span[title]` in it that is not inside a role badge. */
    name: string | null;
}

/** What one reading of the People pane gave. */
export type PeopleTreeReading =
    | { ok: true; count: number; names: string[] }
    /** The list was not in the shape measured; nothing in it is used. */
    | { ok: false; why: string };

const IN_THIS_MEETING = /^In this meeting\s*\((\d{1,4})\)$/i;

const tidy = (text: string): string => text.replace(/\s+/g, ' ').trim();

/**
 * The participants listed under "In this meeting (N)", with N (which counts
 * the VP), the VP's own names left out. Only the shape measured in live
 * meetings is read: a list with any other section header, or with no "In
 * this meeting" header, gives no names, and the caller falls back to what it
 * did without the list. A participant row with no name gives no name, so the
 * list is shorter than N says, and the consent roster treats it as incomplete
 * (consent-roster.ts).
 */
export function parsePeopleTree(rows: PeopleTreeRow[], ownNames: string[] = []): PeopleTreeReading {
    const own = new Set(ownNames.map(normalizeParticipantName).filter((name) => name.length > 0));
    let count: number | null = null;
    let inSection = false;
    const names: string[] = [];
    for (const row of rows) {
        if (row.level === 1) {
            const m = tidy(row.text).match(IN_THIS_MEETING);
            if (!m) return { ok: false, why: 'a section other than "In this meeting"' };
            if (count !== null) return { ok: false, why: 'more than one "In this meeting" section' };
            count = Number(m[1]);
            inSection = true;
            continue;
        }
        if (row.level !== 2 || !inSection) continue;
        const name = tidy(row.name ?? '').slice(0, 100);
        if (!name) continue;
        if (own.has(normalizeParticipantName(name))) continue;
        names.push(name);
    }
    if (count === null || count < 1) return { ok: false, why: 'no "In this meeting" section' };
    return { ok: true, count, names };
}

/** What a check of the chat finds once the People pane has been closed. */
export interface PeopleCloseCheck {
    /** The chat compose box is on screen. */
    composeVisible: boolean;
    /** Teams shows "Chat is turned off for this meeting" in its place. */
    chatTurnedOff: boolean;
}

/**
 * What to do after closing the People pane: nothing when the compose box is
 * there, or when Teams has turned the chat off (which the People pane does not
 * cause, and does not stop the list being read); otherwise one attempt to
 * bring the chat back, and once that has been tried, give up on the People
 * pane for the meeting.
 */
export type PeopleCloseVerdict = 'chat-open' | 'chat-turned-off' | 'recover' | 'give-up';

export function peopleCloseVerdict(check: PeopleCloseCheck, recoveryTried: boolean): PeopleCloseVerdict {
    if (check.composeVisible) return 'chat-open';
    if (check.chatTurnedOff) return 'chat-turned-off';
    return recoveryTried ? 'give-up' : 'recover';
}

/**
 * Whether the People pane may be opened, and when: at most once every
 * PEOPLE_READ_INTERVAL_MS for the list (a request in between is answered with
 * the last list read), and never again once the chat could not be brought
 * back after it (`disable`).
 */
export class PeoplePaneGuard {
    private off = false;

    private lastReadAt: number | null = null;

    private lastNames: string[] | null = null;

    constructor(private readonly clock: () => number = Date.now) {}

    /** The People pane is not opened again in this meeting. */
    get disabled(): boolean {
        return this.off;
    }

    /** The list last read, or null when the last reading gave none. */
    get cached(): string[] | null {
        return this.off ? null : this.lastNames;
    }

    /** For a request for the list: open the pane and read it, answer the last list, or answer none. */
    next(): 'read' | 'cached' | 'off' {
        if (this.off) return 'off';
        if (this.lastReadAt !== null && this.clock() - this.lastReadAt < PEOPLE_READ_INTERVAL_MS) return 'cached';
        return 'read';
    }

    /** A reading was made (or tried): `names` is what it gave, null for nothing. */
    noteRead(names: string[] | null): void {
        this.lastReadAt = this.clock();
        this.lastNames = names;
    }

    disable(): void {
        this.off = true;
        this.lastNames = null;
    }
}
