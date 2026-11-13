/**
 * Reaching the chat on the Teams browser join (teams.ts).
 *
 * The anonymous light-meetings client has no chat button in its in-meeting
 * toolbar: the chat is an item of the toolbar's "More" menu
 * (`data-tid="callingButtons-showMoreBtn"`). The chat pane is usually open
 * when the VP is admitted, but once something else takes its place in the side
 * rail (the People pane opened for the consent flow's participant-list
 * screenshot, or a participant closing it) the only way back to the compose
 * box is through that menu. These are the pure parts of that: which toolbar
 * control is "More", and which item of the open menu is the chat.
 */

/** The toolbar's "More" button on the light-meetings client, then the full client's equivalents. */
export const TEAMS_MORE_BUTTON_SELECTORS = [
    '[data-tid="callingButtons-showMoreBtn"]',
    '#callingButtons-showMoreBtn',
    'button[aria-label="More" i]',
    'button[aria-label="More actions" i]',
];

/** Elements that can be items of the open "More" menu. */
export const TEAMS_MENU_ITEM_SELECTOR =
    '[role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="menu"] button, [role="menu"] [role="option"]';

/** A failed message's "Retry" button in the chat. */
export const TEAMS_FAILED_MESSAGE_RETRY_SELECTOR = '[data-tid="message-failed-retry"]';

/** What is read of one visible item of the open menu. */
export interface MenuItemInfo {
    tid?: string;
    aria?: string;
    text?: string;
}

/** Labels that mention the chat but do something else with it. */
const NOT_OPENING_CHAT = /\b(hide|close|turn off|disable|settings?|pop ?out|leave|mute|bubbles?|captions?|notifications?)\b/i;

/**
 * The index of the menu item that opens the meeting chat, or -1 when the menu
 * has none. An item whose data-tid names the chat is taken first, then one
 * labelled just "Chat" / "Show chat" / "Meeting chat" / "Conversation", then
 * any other whose label mentions the chat and is not one of NOT_OPENING_CHAT.
 */
export function chatMenuItemIndex(items: MenuItemInfo[]): number {
    const label = (item: MenuItemInfo): string => `${item.aria ?? ''} ${item.text ?? ''}`.replace(/\s+/g, ' ').trim();
    const scores = items.map((item) => {
        const tid = item.tid ?? '';
        const text = label(item);
        if (NOT_OPENING_CHAT.test(text) || NOT_OPENING_CHAT.test(tid)) return 0;
        if (/chat|conversation/i.test(tid)) return 3;
        if (/^(show |open )?(the )?(meeting )?(chat|conversation)\b/i.test(item.aria ?? '') || /^(show |open )?(meeting )?(chat|conversation)$/i.test((item.text ?? '').trim())) {
            return 2;
        }
        if (/\b(chat|conversation)\b/i.test(text)) return 1;
        return 0;
    });
    let best = -1;
    for (let i = 0; i < scores.length; i += 1) {
        if (scores[i] > 0 && (best < 0 || scores[i] > scores[best])) best = i;
    }
    return best;
}

// ─── Keeping the chat open ───────────────────────────────────────────────────

/** How often teams.ts checks that the chat pane is open and its reader installed. */
export const TEAMS_CHAT_WATCH_MS = 12_000;

/** The longest wait between attempts at reopening a chat pane that will not open. */
export const TEAMS_CHAT_REOPEN_MAX_BACKOFF_MS = 5 * 60_000;

/**
 * The wait before the next attempt at reopening the chat after `failures`
 * attempts in a row did not open it: none after a success, then one watch
 * interval, doubling up to TEAMS_CHAT_REOPEN_MAX_BACKOFF_MS.
 */
export function chatReopenBackoffMs(failures: number): number {
    if (failures <= 0) return 0;
    return Math.min(TEAMS_CHAT_WATCH_MS * 2 ** (failures - 1), TEAMS_CHAT_REOPEN_MAX_BACKOFF_MS);
}

/** What one check of the chat finds. */
export interface ChatWatchState {
    /** The chat compose box is on screen. */
    paneOpen: boolean;
    /** The page still has the chat reader the handler installed (a reload removes it). */
    readerInstalled: boolean;
    /** Something else is using the side rail or the chat now (an evidence screenshot, a post). */
    busy: boolean;
    /** Attempts in a row that did not reopen the chat. */
    failures: number;
    /** Time since the last attempt at reopening it. */
    msSinceLastAttempt: number;
}

export type ChatWatchAction = 'none' | 'reinstall-reader' | 'reopen-chat';

/**
 * What the periodic chat check does. The reader is put back first, as
 * without it nothing is read even with the pane open; a closed pane is
 * reopened unless something else is using the side rail (the People pane is
 * open for a screenshot, and the chat is reopened after it) or the last
 * attempts failed and their backoff has not passed.
 */
export function chatWatchAction(state: ChatWatchState): ChatWatchAction {
    if (!state.readerInstalled) return 'reinstall-reader';
    if (state.paneOpen || state.busy) return 'none';
    if (state.msSinceLastAttempt < chatReopenBackoffMs(state.failures)) return 'none';
    return 'reopen-chat';
}
