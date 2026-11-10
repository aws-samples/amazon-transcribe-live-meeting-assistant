/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The names a Virtual Participant answers to in the meeting chat, derived from
 * its display name, and how another LMA Virtual Participant's name is told
 * apart from a person's.
 *
 * The display name is the deployment's LMA identity with the owner filled in,
 * `LMA ({LMA_USER})` by default: "LMA (Bob Strahan)", or "LMA
 * (strahanr@example.com)" when the owner is known by an email address. Its
 * handles are the part in parentheses, and the first word of it (the owner's
 * first name, or the local part of the email address), so "LMA (Bob Strahan)"
 * answers to "LMA Bob" and "LMA Bob Strahan", and "LMA (strahanr@example.com)"
 * to "LMA strahanr". A short handle that is also a chat command or question word
 * ("LMA (Help Desk)") is not used, since "LMA help" has to stay the command.
 *
 * Pure and dependency-free apart from the shared text normalisation, so
 * details.ts can build the introduction message from it.
 */
import { normalizeChatText } from './chat-text.js';

/**
 * Words that follow "LMA" as a command or a question, which a short handle
 * must not be. chat-addressing.test.ts holds this to the command tables and the
 * question words, so a new command word cannot be missed here.
 */
export const RESERVED_HANDLE_WORDS: ReadonlySet<string> = new Set([
    // Dismissal, pause, resume, help, ask.
    'leave', 'end', 'stop', 'quit', 'exit', 'goodbye', 'bye', 'pause', 'start', 'help', 'ask',
    // Assistant modes.
    'translate', 'translator', 'interpret', 'assist', 'assistant', 'normal', 'listen', 'listening', 'group',
    'always', 'open', 'wake', 'on',
    // Consent replies.
    'consent', 'no', 'object', 'i',
    // Question words and requests (chat-ask.ts QUESTION_STARTS).
    'what', 'whats', 'who', 'whos', 'whom', 'whose', 'when', 'where', 'why', 'how', 'which',
    'summarize', 'summarise', 'recap', 'explain', 'list', 'tell',
    // Not a name.
    'lma', 'you', 'user', 'the', 'a', 'an',
]);

/** Characters platforms put inside names and mentions that are not part of them. */
const INVISIBLE = /[\u200B-\u200D\u2060\uFEFF]/g;

/** `text` with zero-width characters removed and no-break spaces made ordinary spaces. */
export function cleanChatSpacing(text: string): string {
    return (text || '').replace(INVISIBLE, '').replace(/\u00A0/g, ' ');
}

/** The part of a display name in its last pair of parentheses, or null. */
function parenthesised(displayName: string): string | null {
    const match = /\(([^()]{1,100})\)\s*$/.exec(cleanChatSpacing(displayName).trim());
    return match ? match[1].trim() || null : null;
}

/**
 * Who the display name is for: the part in parentheses, or for a name of the
 * form "LMA <owner>" the words after "LMA". Null for a name that names nobody
 * ("LMA", or a custom identity such as "Acme Notes").
 */
function ownerPart(displayName: string): string | null {
    const inner = parenthesised(displayName);
    if (inner) return inner;
    const words = normalizeChatText(cleanChatSpacing(displayName)).split(' ').filter(Boolean);
    if (words[0] === 'lma' && words.length > 1) return words.slice(1).join(' ');
    return null;
}

/**
 * The handles a display name answers to, as normalised word sequences
 * (`normalizeChatText`), longest first: the owner part whole, then its first
 * word unless that is a reserved word.
 */
export function vpHandles(displayName: string): string[] {
    const owner = ownerPart(displayName || '');
    if (!owner) return [];
    const whole = normalizeChatText(owner);
    if (!whole) return [];
    const handles = [whole];
    // For an email address, the local part; otherwise the first word.
    const first = normalizeChatText(owner.includes('@') ? owner.split('@')[0] : owner.split(/\s+/)[0]);
    if (first && first !== whole && !first.includes(' ') && !RESERVED_HANDLE_WORDS.has(first)) handles.push(first);
    if (handles.length === 1 && !whole.includes(' ') && RESERVED_HANDLE_WORDS.has(whole)) return [];
    return handles;
}

/**
 * The handle to tell people to type after "LMA" to address this VP: the
 * owner's first name or email local part where there is one, else the whole
 * owner part. Null when the display name names nobody.
 */
export function preferredVpHandle(displayName: string): string | null {
    const owner = ownerPart(displayName || '');
    if (!owner) return null;
    const short = owner.includes('@') ? owner.split('@')[0] : owner.split(/\s+/)[0];
    const normalized = normalizeChatText(short);
    if (normalized && !normalized.includes(' ') && !RESERVED_HANDLE_WORDS.has(normalized)) return short.trim();
    return owner.trim();
}

/**
 * Whether a participant's display name is an LMA Virtual Participant's: its
 * first word is "LMA" ("LMA (Alice)", "LMA Alice", "LMA"), ignoring a leading
 * mention "@". A custom identity that does not start with "LMA" cannot be told
 * from a person's name, and is not counted.
 */
export function isLmaVpName(name: string | null | undefined): boolean {
    const words = normalizeChatText(cleanChatSpacing(name || '')).split(' ');
    return words[0] === 'lma';
}

/**
 * Whether `name` is the display name `displayName`, compared normalised, so a
 * mention ("@LMA (Bob)") or other spacing still matches.
 */
export function sameVpName(name: string | null | undefined, displayName: string | null | undefined): boolean {
    const a = normalizeChatText(cleanChatSpacing(name || ''));
    return a.length > 0 && a === normalizeChatText(cleanChatSpacing(displayName || ''));
}

/**
 * A chat message with any parenthesised address after its "LMA" addressee
 * removed: "LMA (Alice) leave" reads as "LMA leave". For deciding whether a
 * message addressed to some LMA is a command (meeting-chat.ts); which LMA it is
 * for is decided in chat-addressing.ts.
 */
export function withoutVpAddress(message: string): string {
    return cleanChatSpacing(message || '').replace(/(^|[^\p{L}\p{N}])(@?lma)\s*\([^()]{1,100}\)/iu, '$1$2');
}

/** The other LMA VPs' display names, as chat-addressing.ts knows them; see `setVpPeerNames`. */
let peerNames: () => readonly string[] = () => [];

/**
 * Where `addressingVpHandle` reads the other VPs from. chat-addressing.ts sets
 * it, so details.ts and chat-help.ts can name this VP without importing it.
 */
export function setVpPeerNames(names: () => readonly string[]): void {
    peerNames = names;
}

/**
 * The name to tell people to add after "LMA" to address this VP (the intro's
 * `{LMA_HANDLE}`, the help reply): `preferredVpHandle`, unless another VP in
 * the meeting answers to that short handle too (two VPs for two Bobs), in which
 * case the owner part whole in parentheses, "(Bob Strahan)", which is this VP's
 * alone. The display name itself when it names nobody in particular.
 */
export function addressingVpHandle(displayName: string, peers: readonly string[] = peerNames()): string {
    const short = preferredVpHandle(displayName);
    if (!short) return displayName;
    const others = new Set(
        peers.filter((peer) => !sameVpName(peer, displayName)).flatMap((peer) => vpHandles(peer)),
    );
    if (!others.has(normalizeChatText(short))) return short;
    const owner = ownerPart(displayName);
    return owner ? `(${owner.trim()})` : displayName;
}
