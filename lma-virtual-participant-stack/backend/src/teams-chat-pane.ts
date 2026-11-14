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

// ─── Watching the chat ───────────────────────────────────────────────────────

/** How often teams.ts checks that the chat pane is open and its reader installed. */
export const TEAMS_CHAT_WATCH_MS = 12_000;

/** What one check of the chat finds. */
export interface ChatWatchState {
    /** The chat compose box is on screen. */
    paneOpen: boolean;
    /** The page still has the chat reader the handler installed (a reload removes it). */
    readerInstalled: boolean;
    /** A post holds the chat pane now. */
    busy: boolean;
    /** The pane was found closed, and that was logged, at an earlier check. */
    closedReported: boolean;
}

export type ChatWatchAction = 'none' | 'reinstall-reader' | 'report-closed' | 'report-reopened';

/**
 * What the periodic chat check does. It clicks nothing in the meeting. The
 * reader is put back first, as without it nothing is read even with the pane
 * open. Nothing else is done while a post holds the pane. A pane found closed
 * is reported once (and reported again only after it has been seen open), and
 * a pane seen open again after that is reported as back.
 */
export function chatWatchAction(state: ChatWatchState): ChatWatchAction {
    if (!state.readerInstalled) return 'reinstall-reader';
    if (state.busy) return 'none';
    if (state.paneOpen) return state.closedReported ? 'report-reopened' : 'none';
    return state.closedReported ? 'none' : 'report-closed';
}
