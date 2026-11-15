/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Keeping the chat pane open on the Teams browser join (teams.ts).
 *
 * The VP reads and posts the chat through Teams' chat pane, which shares the
 * side rail with the People pane. On the anonymous light-meetings client,
 * opening the People pane puts it in the chat's place, and closing it again
 * does not bring the chat back. That client has no chat button in its
 * toolbar, and its "More" menu holds no chat item either (it has Meeting
 * info, Devices, Video effects and settings, Captions, Call me, Show chat
 * bubbles and Accessibility). So the VP keeps the chat open by never opening
 * the People pane (the consent evidence there is the chat screenshot only),
 * never opens the More menu (every item of it is a setting or a toggle), and
 * clicks a toolbar chat button only on a client that has one
 * (isChatPaneButton). A chat pane closed from the meeting is reported, not
 * reopened.
 */

/** A failed message's "Retry" button in the chat. */
export const TEAMS_FAILED_MESSAGE_RETRY_SELECTOR = '[data-tid="message-failed-retry"]';

/**
 * How long a Retry button seen right after a post is given to go away before
 * the post counts as failed. On the anonymous light-meetings client a Retry
 * button showed after every post in a live meeting, every message of which
 * reached the chat, and was gone before the next post: it is shown while a
 * message is still being sent, not only once it has failed.
 */
export const TEAMS_FAILED_MARKER_SETTLE_MS = 5000;

/** What is read of a candidate toolbar chat button before it is clicked. */
export interface ChatButtonInfo {
    aria?: string;
    title?: string;
    text?: string;
    role?: string;
    /** The element sits inside an open menu (`[role="menu"]`). */
    inMenu?: boolean;
}

/** Labels that mention the chat but are not the button that shows the chat pane. */
const NOT_THE_CHAT_PANE = /\b(bubbles?|hide|close|turn off|disable|settings?|pop ?out|captions?|notifications?|leave)\b/i;

/**
 * Whether a control matched by a chat-button selector is a toolbar button that
 * shows the chat pane. Menu items are never chosen (the More menu holds only
 * settings and toggles, such as "Show chat bubbles"), nor is a control whose
 * label says it hides, closes or configures the chat.
 */
export function isChatPaneButton(info: ChatButtonInfo): boolean {
    if (info.inMenu) return false;
    if (/^menuitem/i.test(info.role ?? '')) return false;
    const label = `${info.aria ?? ''} ${info.title ?? ''} ${info.text ?? ''}`.replace(/\s+/g, ' ').trim();
    return !NOT_THE_CHAT_PANE.test(label);
}

// ─── Posting ─────────────────────────────────────────────────────────────────

/** How much of the start of a message must be in the compose box before it is submitted. */
export const COMPOSER_MATCH_PREFIX = 20;

const squash = (text: string): string => text.replace(/\s+/g, ' ').trim();

/**
 * Whether the compose box holds `message`, judged by its first
 * COMPOSER_MATCH_PREFIX characters with runs of whitespace treated as one
 * space. An empty compose box never holds a message: teams.ts does not
 * submit it, and reports the message as not posted.
 */
export function composerHoldsMessage(composerText: string, message: string): boolean {
    const want = squash(message).slice(0, COMPOSER_MATCH_PREFIX);
    const have = squash(composerText);
    return want.length > 0 && have.length > 0 && have.includes(want);
}

/** What is read of an element before it is typed into or clicked as part of a post. */
export interface PostTargetInfo {
    /** `HTMLElement.isContentEditable`. */
    editable?: boolean;
    role?: string;
    aria?: string;
    title?: string;
    tid?: string;
    name?: string;
    text?: string;
}

/** Whether an element is a compose box the VP may type into: editable text, nothing else. */
export function isComposeBox(info: PostTargetInfo): boolean {
    return info.editable === true || (info.role ?? '').toLowerCase() === 'textbox';
}

/** Whether an element is the chat's Send button, so a resolver's guess is clicked only when it is. */
export function isSendButton(info: PostTargetInfo): boolean {
    if (/^menuitem/i.test(info.role ?? '')) return false;
    const label = `${info.aria ?? ''} ${info.title ?? ''} ${info.tid ?? ''} ${info.name ?? ''} ${info.text ?? ''}`;
    return /send/i.test(label) && !/\b(bubbles?|settings?|view|layout)\b/i.test(label);
}

// ─── Watching the chat ───────────────────────────────────────────────────────

/** How often teams.ts checks that the chat pane is open and its reader installed. */
export const TEAMS_CHAT_WATCH_MS = 12_000;

/** What one check of the chat finds. */
export interface ChatWatchState {
    /** The chat compose box is on screen. */
    paneOpen: boolean;
    /** The page still has the chat reader the handler installed (a reload removes it). */
    readerInstalled: boolean;
    /** The pane was found closed, and that was logged, at an earlier check. */
    closedReported: boolean;
}

export type ChatWatchAction = 'none' | 'reinstall-reader' | 'report-closed' | 'report-reopened';

/**
 * What the periodic chat check does. It clicks nothing in the meeting. The
 * reader is put back first, as without it nothing is read even with the pane
 * open. A pane found closed is reported once (and reported again only after
 * it has been seen open), and a pane seen open again after that is reported
 * as back. teams.ts skips the check while a post or a screenshot holds the
 * pane (see AsyncLock).
 */
export function chatWatchAction(state: ChatWatchState): ChatWatchAction {
    if (!state.readerInstalled) return 'reinstall-reader';
    if (state.paneOpen) return state.closedReported ? 'report-reopened' : 'none';
    return state.closedReported ? 'none' : 'report-closed';
}

// ─── One user of the chat pane at a time ─────────────────────────────────────

/**
 * A first-come, first-served async mutex. teams.ts runs every post (typing
 * and submitting), the consent evidence screenshots and each chat-watch check
 * under one of these, so a screenshot never catches a half-typed message, a
 * post waits for a screenshot to finish, two posts never type into the
 * composer at once, and the chat watch never acts while either is in
 * progress (it skips its turn rather than waiting).
 */
export class AsyncLock {
    private held = false;

    private readonly waiting: Array<() => void> = [];

    /** Whether the lock is held (nobody waits while it is not). */
    get busy(): boolean {
        return this.held;
    }

    /** Waits for the lock; resolves to its release, which may be called more than once. */
    acquire(): Promise<() => void> {
        return new Promise((resolve) => {
            const grant = (): void => {
                this.held = true;
                resolve(this.releaser());
            };
            if (this.held) this.waiting.push(grant);
            else grant();
        });
    }

    /** Runs `fn` holding the lock, releasing it however `fn` ends. */
    async run<T>(fn: () => Promise<T>): Promise<T> {
        const release = await this.acquire();
        try {
            return await fn();
        } finally {
            release();
        }
    }

    /** Runs `fn` holding the lock if it is free now; otherwise answers `whenBusy` without running it. */
    async runIfFree<T, B>(fn: () => Promise<T>, whenBusy: B): Promise<T | B> {
        if (this.held) return whenBusy;
        this.held = true;
        const release = this.releaser();
        try {
            return await fn();
        } finally {
            release();
        }
    }

    private releaser(): () => void {
        let released = false;
        return () => {
            if (released) return;
            released = true;
            const next = this.waiting.shift();
            if (next) next();
            else this.held = false;
        };
    }
}

// ─── A chat that Teams turns off ─────────────────────────────────────────────

/**
 * Shown in place of the compose box once Teams has turned the chat off for
 * the VP ("Chat is turned off for this meeting"). On the anonymous
 * light-meetings client a guest's chat can be turned off shortly after it is
 * admitted, by a setting of the meeting's organization, even when the
 * meeting's own Meeting chat setting is On.
 */
export const TEAMS_CHAT_TURNED_OFF_SELECTOR = '[data-tid="compose-disabled-placeholder-text"]';

/** Whether the page is the anonymous light-meetings client. */
export function isLightMeetingsUrl(url: string): boolean {
    return /\/light-meetings(\/|\?|$)/i.test(url);
}

/** Said in the introduction, on the light-meetings client, instead of the chat commands. */
export const CHATLESS_REMOVAL_LINE = 'A meeting organizer can remove me from the participant list.';

/**
 * A sentence that gives an LMA chat command: one quoting LMA followed by a
 * command ("LMA leave", "@LMA help"). A quoted "LMA" on its own is not a
 * command, nor is a sentence that only mentions the chat.
 */
const CHAT_COMMAND_SENTENCE = /"@?LMA\b[\s,]+[^"\s][^"]*"/i;

/**
 * The introduction for the light-meetings client: the sentences that give a
 * chat command are left out, as that chat may be turned off moments after
 * the introduction is posted, and the first of them is replaced by
 * CHATLESS_REMOVAL_LINE. Messages that give no chat command are kept as
 * written.
 */
export function introForChatlessClient(messages: string[]): string[] {
    let replaced = false;
    const out: string[] = [];
    for (const message of messages) {
        const sentences = squash(message).split(/(?<=[.!?])\s+(?=\S)/);
        const kept: string[] = [];
        let dropped = false;
        for (const sentence of sentences) {
            if (!CHAT_COMMAND_SENTENCE.test(sentence)) {
                kept.push(sentence);
                continue;
            }
            dropped = true;
            if (!replaced) {
                kept.push(CHATLESS_REMOVAL_LINE);
                replaced = true;
            }
        }
        if (!dropped) out.push(message);
        else if (kept.length > 0) out.push(kept.join(' '));
    }
    return out;
}

// ─── Reading the chat ────────────────────────────────────────────────────────

/** One message as the chat reader (teams.ts, __lmaScrapeMessages) found it. */
export interface ScrapedChatMessage {
    text: string;
    id: string | null;
    author: string | null;
    time: string | null;
    links: string[];
}

/** How long a message's other copy is looked for in a later read. */
export const CHAT_COPY_WINDOW_MS = 15_000;

/**
 * The anonymous light-meetings client renders each chat message twice: its
 * body ("Great evening!"), and a heading that repeats it with its sender
 * ("Great evening! by Strahan, Bob"). Seen in a live meeting, where every
 * message reached the transcript twice, once with no sender. This makes one
 * message of the two: the body's text, with the sender Teams shows on the
 * heading's row.
 *
 * A reading is a heading only when the reader found its author on the row
 * and its text ends in " by <author>"; the sender is always that author,
 * never a name taken from a message's text. A message that only ends in
 * "by <name>" is kept as it is.
 *
 * The two copies of a message can be read in different polls, so each copy
 * read without its other half is remembered for CHAT_COPY_WINDOW_MS, and is
 * paired at most once: a heading first completes the oldest body still
 * waiting for one, then a body in the same read; a body first matches the
 * oldest heading still waiting for one. Two bodies with the same text are
 * never merged, and a message whose body was read before its heading keeps
 * no sender (it has been passed on by then).
 */
export class ChatCopyMerger {
    /** Bodies passed on with no sender, waiting for their heading. */
    private bodies: Array<{ text: string; at: number }> = [];

    /** Headings passed on as messages, waiting for their body. */
    private headings: Array<{ text: string; at: number }> = [];

    constructor(private readonly clock: () => number = Date.now) {}

    merge(read: ScrapedChatMessage[]): ScrapedChatMessage[] {
        const now = this.clock();
        this.bodies = this.bodies.filter((b) => now - b.at <= CHAT_COPY_WINDOW_MS);
        this.headings = this.headings.filter((h) => now - h.at <= CHAT_COPY_WINDOW_MS);
        const take = (list: Array<{ text: string; at: number }>, text: string): boolean => {
            const at = list.findIndex((entry) => entry.text === text);
            if (at < 0) return false;
            list.splice(at, 1);
            return true;
        };
        // In the order the page lists them, so a copy pairs with its own.
        const items = read.map((message) => {
            const suffix = message.author ? ` by ${message.author}` : '';
            const body = suffix && message.text.endsWith(suffix) ? message.text.slice(0, -suffix.length).trim() : '';
            return { message: { ...message }, heading: body ? { body, sender: message.author as string } : null, done: false };
        });
        const out = new Map<number, ScrapedChatMessage>();
        items.forEach((item, index) => {
            if (!item.heading) return;
            // Its body was passed on in an earlier read.
            if (take(this.bodies, item.heading.body)) {
                item.done = true;
                return;
            }
            const body = items.findIndex((other) => !other.heading && !other.done && !other.message.author && other.message.text === item.heading?.body);
            if (body >= 0) {
                items[body].done = true;
                item.done = true;
                out.set(body, { ...items[body].message, author: item.heading.sender });
                return;
            }
            item.done = true;
            out.set(index, { ...item.message, text: item.heading.body, author: item.heading.sender });
            this.headings.push({ text: item.heading.body, at: now });
        });
        items.forEach((item, index) => {
            if (item.done) return;
            // Its heading was passed on as the message in an earlier read.
            if (!item.message.author && take(this.headings, item.message.text)) return;
            if (!item.message.author) this.bodies.push({ text: item.message.text, at: now });
            out.set(index, item.message);
        });
        return [...out.entries()].sort((a, b) => a[0] - b[0]).map(([, message]) => message);
    }
}
