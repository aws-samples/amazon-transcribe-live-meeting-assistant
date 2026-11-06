/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which LMA Virtual Participant a chat message is for, when there may be more
 * than one in the meeting.
 *
 * Every chat command, question and addressed consent reply names "LMA". With
 * two VPs in a meeting (two people each invited theirs), "LMA leave" would
 * otherwise remove both. So, before a handler reads a message for its
 * commands, `route` decides whether this VP acts on it:
 *
 * - **Addressed to this VP by name** — its display name, typed or as a
 *   platform mention ("@LMA (Bob) leave", "LMA (Bob) summarize"), or "LMA"
 *   followed by one of its handles (vp-handle.ts: "LMA Bob leave", "@LMA
 *   strahanr help"): it acts, and the message is read with the address
 *   reduced to "LMA", so every matcher sees the form it already knows.
 * - **Addressed to another VP** — a parenthesised name after "LMA" that is not
 *   this VP's, another VP's display name, or one of another VP's handles: it
 *   does not act.
 * - **The bare "LMA" / "@LMA"** — it acts only when it knows of no other LMA
 *   VP in the meeting. With several it stays silent (it does not post the help
 *   either): any reply would be one of several.
 * - **Not addressed to LMA at all** — an unaddressed consent reply ("I
 *   agree"), ordinary chat: it reads it as before.
 *
 * A message from another LMA VP (by its sender name) is never acted on.
 *
 * The other VPs are known by name from what this VP can see: chat senders, the
 * names the platform shows as speaking (consent-roster.ts's sightings), and
 * where a handler can read the whole participant list (the Google Meet tiles,
 * the Teams Azure Communication Services roster), that list. A name whose first
 * word is "LMA" (vp-handle.ts `isLmaVpName`) is a VP. One that has not yet
 * written in the chat or been seen by name cannot be counted on the platforms
 * with no list; each VP posts its introduction when it joins, which is how the
 * VPs already present see a new one.
 *
 * Mentions: the Teams clients render a mention as the mentioned name with no
 * "@" (and adjacent mention spans run together, "LMA(Bob)"); Zoom shows the
 * "@" before the name. Both are handled as typed names; zero-width characters
 * and no-break spaces inside them are ignored.
 */
import { normalizeChatText } from './chat-text.js';
import { onParticipantSeen } from './consent-roster.js';
import { details } from './details.js';
import { createLogger } from './logger.js';
import { cleanChatSpacing, isLmaVpName, sameVpName, vpHandles } from './vp-handle.js';

const log = createLogger('chat-addressing');

/** Who a message is addressed to. */
export type ChatAddressKind = 'self' | 'other' | 'bare' | 'none';

export interface ChatAddress {
    kind: ChatAddressKind;
    /** The message with the address reduced to "LMA" (unchanged for `bare` and `none`). */
    text: string;
}

/** What a handler does with a message. */
export interface ChatRoute extends ChatAddress {
    /** Whether this VP reads the message for its commands, questions and consent replies. */
    act: boolean;
    /** Why it does not, for the debug line. */
    reason?: 'other-vp' | 'several-vps' | 'from-vp';
}

function escapeRegExp(text: string): string {
    return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * A pattern for a display name typed or mentioned in a message: an optional
 * "@" (and an optional "LMA " before a name that does not start with it), any
 * spacing, any case, parentheses with or without spaces around them.
 */
function namePattern(displayName: string): RegExp | null {
    const words = cleanChatSpacing(displayName)
        .trim()
        .split(/(\s+|[()])/)
        .map((part) => part.trim())
        .filter(Boolean);
    if (words.length === 0) return null;
    const body = words.map(escapeRegExp).join('\\s*');
    return new RegExp(`(^|[^\\p{L}\\p{N}@])(?:@?lma\\s+)?@?${body}(?![\\p{L}\\p{N}])`, 'iu');
}

/** A pattern for a handle (normalised words) right after the "LMA" addressee. */
function handlePattern(handle: string): RegExp {
    const words = handle.split(' ').filter(Boolean).map(escapeRegExp);
    return new RegExp(`^[\\s,:;.!-]*${words.join('[\\s._@,-]+')}(?![\\p{L}\\p{N}])`, 'iu');
}

/** Replace the first match of `pattern` (whose group 1 is the boundary before it) with "LMA". */
function replaceWithLma(message: string, pattern: RegExp): string | null {
    const match = pattern.exec(message);
    if (!match) return null;
    const start = match.index + match[1].length;
    return `${message.slice(0, start)}LMA${message.slice(match.index + match[0].length)}`;
}

const LMA_TOKEN = /(^|[^\p{L}\p{N}])@?lma(?![\p{L}\p{N}])/iu;

/**
 * Who `message` is addressed to, for a VP whose display name is `self`, with
 * `peers` the display names of the other LMA VPs it knows of.
 */
export function resolveChatAddress(message: string, self: string, peers: readonly string[]): ChatAddress {
    const clean = cleanChatSpacing(message || '');

    // 1. A whole display name, this VP's or another's. A display name that is
    // only "LMA" names nobody in particular.
    const named = (name: string) => normalizeChatText(name) !== 'lma' && normalizeChatText(name).length > 0;
    if (named(self)) {
        const pattern = namePattern(self);
        const text = pattern && replaceWithLma(clean, pattern);
        if (text !== null && text !== undefined) return { kind: 'self', text };
    }
    for (const peer of peers) {
        if (!named(peer) || sameVpName(peer, self)) continue;
        const pattern = namePattern(peer);
        const text = pattern && replaceWithLma(clean, pattern);
        if (text !== null && text !== undefined) return { kind: 'other', text };
    }

    // 2. The "LMA" addressee, and what follows it.
    const token = LMA_TOKEN.exec(clean);
    if (!token) return { kind: 'none', text: clean };
    const end = token.index + token[0].length;
    const before = clean.slice(0, end);
    const after = clean.slice(end);

    // A parenthesised name: this VP's when it is its owner part or a handle.
    const paren = /^\s*\(([^()]{1,100})\)/.exec(after);
    if (paren) {
        const inner = normalizeChatText(paren[1]);
        const mine = vpHandles(self).includes(inner);
        return { kind: mine ? 'self' : 'other', text: `${before}${after.slice(paren[0].length)}` };
    }

    // A handle: this VP's, then the others'. Longest first within each.
    for (const [kind, names] of [['self', [self]], ['other', peers.filter((p) => !sameVpName(p, self))]] as const) {
        const handles = names.flatMap((name) => vpHandles(name)).sort((a, b) => b.length - a.length);
        for (const handle of handles) {
            const match = handlePattern(handle).exec(after);
            if (match) return { kind, text: `${before}${after.slice(match[0].length)}` };
        }
    }
    return { kind: 'bare', text: clean };
}

/**
 * Whether a VP whose display name is `self`, knowing of `peers`, acts on
 * `message` from `sender`, and the text it reads it as. Pure; see `ChatAddressing`.
 */
export function routeChatMessage(
    message: string,
    sender: string | null | undefined,
    self: string,
    peers: readonly string[],
): ChatRoute {
    const address = resolveChatAddress(message, self, peers);
    if (sender && isLmaVpName(sender) && normalizeChatText(sender) !== 'lma' && !sameVpName(sender, self)) {
        return { ...address, act: false, reason: 'from-vp' };
    }
    if (address.kind === 'other') return { ...address, act: false, reason: 'other-vp' };
    if (address.kind === 'bare' && peers.some((peer) => !sameVpName(peer, self))) {
        return { ...address, act: false, reason: 'several-vps' };
    }
    return { ...address, act: true };
}

/**
 * The other LMA VPs this one knows of, and the routing of each chat message.
 * One per meeting; the handlers use `chatAddressing`.
 */
export class ChatAddressing {
    /** Seen by name in the chat or as a speaker, by normalised name. */
    private readonly seen = new Map<string, string>();
    /** The platform's whole participant list, where a handler reads one. */
    private roster: string[] | null = null;
    private lastCount = 0;

    constructor(
        private readonly selfName: () => string,
        private readonly exitMessages: () => readonly string[] = () => [],
    ) {}

    /**
     * Whether `name` is another LMA VP's. A name that is only "LMA" is not
     * counted: the handlers already take a sender of that name for this VP
     * itself (chat-ask.ts `isOwnChatSender`).
     */
    private isPeer(name: string | null | undefined): name is string {
        return !!name && isLmaVpName(name) && normalizeChatText(name) !== 'lma' && !sameVpName(name, this.selfName());
    }

    /** A participant name seen in the meeting (a chat sender, a speaker). */
    notePeer(name: string | null | undefined): void {
        if (!this.isPeer(name)) return;
        const key = normalizeChatText(name);
        if (!this.seen.has(key)) {
            this.seen.set(key, cleanChatSpacing(name).trim());
            this.logCount();
        }
    }

    /**
     * The platform's whole participant list, or null when it could not be
     * read. While a list is read it decides who is present, so a VP that left
     * stops counting.
     */
    noteRoster(names: readonly string[] | null): void {
        this.roster = names === null ? null : names.filter((name) => this.isPeer(name)).map((name) => name.trim());
        this.logCount();
    }

    /** The other LMA VPs' display names. */
    peers(): string[] {
        if (this.roster !== null) return [...this.roster];
        return [...this.seen.values()];
    }

    /** Whether this VP acts on `message` from `sender`, and the text it reads it as. */
    route(message: string, sender?: string | null): ChatRoute {
        this.notePeer(sender);
        const route = routeChatMessage(message, sender, this.selfName(), this.peers());
        // A VP that posts its exit message has left.
        if (route.reason === 'from-vp' && sender) {
            const text = normalizeChatText(message);
            if (this.exitMessages().some((exit) => normalizeChatText(exit) === text)) {
                this.seen.delete(normalizeChatText(sender));
                this.logCount();
            }
        }
        if (!route.act) log.debug(`[chat-addressing] not for this VP (${route.reason}, address=${route.kind})`);
        return route;
    }

    private logCount(): void {
        const count = this.peers().length;
        if (count === this.lastCount) return;
        this.lastCount = count;
        log.info(
            count === 0
                ? '[chat-addressing] no other LMA Virtual Participant is in the meeting; "LMA <command>" is for this one'
                : `[chat-addressing] ${count} other LMA Virtual Participant(s) in the meeting; `
                    + 'acting only on messages addressed to this one by name',
        );
    }
}

/** This VP's routing, by its display name. */
export const chatAddressing = new ChatAddressing(
    () => details.scribeIdentity,
    () => details.exitMessages,
);

// The names the platform shows as speaking.
onParticipantSeen((name) => chatAddressing.notePeer(name));
