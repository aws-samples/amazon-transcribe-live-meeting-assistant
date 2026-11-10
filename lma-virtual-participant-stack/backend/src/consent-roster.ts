/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Who is present in the meeting, as the consent flow lists them on the
 * meeting's consent record (setMeetingConsentParticipants).
 *
 * Under the opt-in rule "each participant consents" this list decides the
 * gate: every name on it has to have consented. So it must never be shorter
 * than the meeting is, and the platforms differ in what they show:
 *
 * - **A roster of names.** The Teams Azure Communication Services join reads
 *   the call's participant list. Where a handler can read one, and it has as
 *   many names as the attendee count says, it is the list.
 * - **A count, and the names the VP has seen.** The browser joins know how
 *   many attendees there are (the same reading that makes the VP leave when it
 *   is alone), and see names only as the active speaker and as chat senders.
 *   Then the list is the names seen since the count last went up, plus one
 *   "Unidentified participant N" for each seat no name accounts for. An
 *   unidentified participant can never consent, so the meeting is not granted
 *   until everyone present has spoken or written in the chat, and consented.
 * - **Neither.** Where a platform gives no count and no list (the Webex
 *   browser join), the names seen can never be shown to be everyone, so one
 *   unidentified participant is always listed and the each-participant rule
 *   is never met from the chat. The owner-attests rule works there.
 *
 * When the count goes up, a name seen before may belong to somebody who has
 * since left, so the names seen before stop counting: after somebody joins,
 * everyone present has to be seen again. When it goes down while consent is
 * still pending the same is true, because the person who left may be one of
 * the names; while consent is granted it is not, because everybody present was
 * already accounted for and has consented, and a departure cannot add anyone
 * who has not.
 *
 * One thing this cannot see: one person leaving and another joining between
 * two readings of the count. The count is read every few seconds, so the
 * window is that long; the docs say so.
 *
 * The VP's own identities are never listed.
 */

import { normalizeParticipantName } from './consent-matcher.js';

export const UNIDENTIFIED_PARTICIPANT = 'Unidentified participant';

export type RosterChange = 'joined' | 'left' | null;

export class ConsentRoster {
    private readonly own: Set<string>;

    private readonly clock: () => number;

    /** Each name seen, keyed by its normalised form, with when it was last seen. */
    private readonly sightings = new Map<string, { name: string; at: number }>();

    /** Names last seen before this instant no longer count as present. */
    private floor = 0;

    /** The attendee count, VP included; null where the platform gives none. */
    private count: number | null = null;

    /** The platform's own participant list, VP left out; null where there is none. */
    private roster: string[] | null = null;

    constructor(ownIdentities: string[], clock: () => number = Date.now) {
        this.own = new Set(ownIdentities.map(normalizeParticipantName).filter((name) => name.length > 0));
        this.clock = clock;
    }

    /** Whether `name` is exactly one of the VP's own configured identities. */
    isOwnIdentity(name: string | null | undefined): boolean {
        const key = normalizeParticipantName(name);
        return key.length > 0 && this.own.has(key);
    }

    /**
     * Whether `name` is not a participant to list: one of the VP's own
     * identities, or a placeholder a speaker indicator shows when nobody is
     * named ("none", "unknown").
     */
    isOwn(name: string | null | undefined): boolean {
        const key = normalizeParticipantName(name);
        return key.length === 0 || this.own.has(key) || key === 'none' || key === 'unknown';
    }

    /**
     * A name shown as speaking or sending a chat message. Answers whether that
     * changes who is listed as present: a name not seen before, or not seen
     * since the count last changed.
     */
    noteSeen(name: string | null | undefined): boolean {
        if (!name || this.isOwn(name) || isPlaceholder(name)) return false;
        const display = name.replace(/\s+/g, ' ').trim().slice(0, 100);
        const key = normalizeParticipantName(display);
        const previous = this.sightings.get(key);
        // Strictly after the floor even when the clock has not moved since it
        // was set, so a name seen right after a change counts.
        // The first spelling seen is the one listed, so the list does not
        // change only because a platform wrote a name differently.
        this.sightings.set(key, { name: previous?.name ?? display, at: Math.max(this.clock(), this.floor + 1) });
        return !previous || previous.at <= this.floor;
    }

    /**
     * The attendee count, VP included, as the platform handler last read it.
     * `granted` is whether the meeting's consent is granted now.
     */
    noteCount(count: number, granted: boolean): RosterChange {
        if (!Number.isFinite(count) || count < 1) return null;
        const previous = this.count;
        this.count = Math.floor(count);
        if (previous === null || this.count === previous) return null;
        if (this.count > previous) {
            this.resetSightings();
            return 'joined';
        }
        if (!granted) this.resetSightings();
        return 'left';
    }

    /** Names seen until now stop counting as present. Monotonic, so a frozen clock still moves it. */
    private resetSightings(): void {
        this.floor = Math.max(this.clock(), this.floor + 2);
    }

    /**
     * The platform's participant list, VP left out, or null when it could not
     * be read. Answers whether somebody new is on it.
     */
    noteRoster(names: string[] | null): RosterChange {
        if (names === null) {
            this.roster = null;
            return null;
        }
        const cleaned = dedupe(names.filter((name) => !this.isOwn(name) && !isPlaceholder(name)));
        const before = new Set((this.roster ?? []).map(normalizeParticipantName));
        const hadRoster = this.roster !== null;
        this.roster = cleaned;
        for (const name of cleaned) this.noteSeen(name);
        if (!hadRoster) return null;
        if (cleaned.some((name) => !before.has(normalizeParticipantName(name)))) return 'joined';
        if (cleaned.length < before.size) return 'left';
        return null;
    }

    /** Seats other than the VP's, when the platform gives a count. */
    get seats(): number | null {
        return this.count === null ? null : Math.max(0, this.count - 1);
    }

    /** Whether the platform's own list accounts for everybody the count says is there. */
    get rosterIsComplete(): boolean {
        return this.roster !== null && this.roster.length > 0 && (this.seats === null || this.roster.length >= this.seats);
    }

    /**
     * Everyone present, by display name. With `withPlaceholders`, a seat no
     * name accounts for is listed as an unidentified participant, which is
     * what makes the each-participant rule wait for them.
     */
    present(withPlaceholders: boolean): string[] {
        if (this.rosterIsComplete) return [...(this.roster as string[])];
        const seen = [...this.sightings.values()]
            .filter((entry) => entry.at > this.floor)
            .sort((a, b) => a.at - b.at)
            .map((entry) => entry.name);
        const names = dedupe([...(this.roster ?? []), ...seen]);
        const seats = this.seats;
        if (withPlaceholders && seats !== null) {
            const taken = new Set(names.map(normalizeParticipantName));
            for (let n = 1; names.length < seats; n += 1) {
                const label = `${UNIDENTIFIED_PARTICIPANT} ${n}`;
                if (!taken.has(normalizeParticipantName(label))) names.push(label);
            }
        } else if (withPlaceholders) {
            // No count and no complete list: nobody can say the names seen are
            // everyone, so somebody is always unaccounted for.
            names.push(`${UNIDENTIFIED_PARTICIPANT} 1`);
        }
        return names;
    }
}

/**
 * Whether a display name reads as one of the list's placeholders. Such a name
 * is never listed from a sighting or a platform list: it would merge with the
 * placeholder it reads as, and one seat would be counted for two people.
 */
function isPlaceholder(name: string): boolean {
    return normalizeParticipantName(name).startsWith(normalizeParticipantName(UNIDENTIFIED_PARTICIPANT));
}

function dedupe(names: string[]): string[] {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const raw of names) {
        const name = raw.replace(/\s+/g, ' ').trim().slice(0, 100);
        const key = normalizeParticipantName(name);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        out.push(name);
    }
    return out;
}

/**
 * Names the meeting platform shows as speaking, for the consent roster.
 *
 * Dependency-free so the transcription service, which every handler already
 * tells who is speaking, can report them without importing the consent flow.
 */
type SightingListener = (name: string) => void;
const sightingListeners = new Set<SightingListener>();

export function noteParticipantSeen(name: string): void {
    for (const listener of sightingListeners) {
        try {
            listener(name);
        } catch {
            /* a listener must never break speaker tracking */
        }
    }
}

export function onParticipantSeen(listener: SightingListener): () => void {
    sightingListeners.add(listener);
    return () => sightingListeners.delete(listener);
}
