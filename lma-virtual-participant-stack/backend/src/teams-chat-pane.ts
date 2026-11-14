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
