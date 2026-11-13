/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reaching the chat on the Teams browser join (teams-chat-menu.ts): which item
 * of the toolbar's "More" menu opens the chat, and when the periodic chat
 * check reopens the pane. The clicks themselves need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
    chatMenuItemIndex,
    chatReopenBackoffMs,
    chatWatchAction,
    ChatWatchState,
    TEAMS_CHAT_REOPEN_MAX_BACKOFF_MS,
    TEAMS_CHAT_WATCH_MS,
    TEAMS_FAILED_MESSAGE_RETRY_SELECTOR,
    TEAMS_MORE_BUTTON_SELECTORS,
} from './teams-chat-menu.js';

test('the "More" button of the light-meetings toolbar is tried first', () => {
    assert.equal(TEAMS_MORE_BUTTON_SELECTORS[0], '[data-tid="callingButtons-showMoreBtn"]');
    assert.equal(TEAMS_FAILED_MESSAGE_RETRY_SELECTOR, '[data-tid="message-failed-retry"]');
});

test('the menu item labelled Chat is chosen', () => {
    const items = [{ aria: 'Raise' }, { text: 'Reactions' }, { aria: 'Chat', text: 'Chat' }, { text: 'Settings' }];
    assert.equal(chatMenuItemIndex(items), 2);
});

test('an item whose data-tid names the chat is preferred over one that only mentions it', () => {
    const items = [{ text: 'Open meeting notes and chat history' }, { tid: 'callingButtons-chatBtn', text: 'Chat' }];
    assert.equal(chatMenuItemIndex(items), 1);
});

test('"Show chat", "Meeting chat" and "Conversation" are recognised', () => {
    assert.equal(chatMenuItemIndex([{ text: 'View' }, { aria: 'Show chat' }]), 1);
    assert.equal(chatMenuItemIndex([{ text: 'Meeting chat' }]), 0);
    assert.equal(chatMenuItemIndex([{ aria: 'Show conversation' }]), 0);
});

test('items that hide, close or configure the chat are not chosen', () => {
    assert.equal(chatMenuItemIndex([{ aria: 'Hide chat' }, { text: 'Close chat' }, { text: 'Chat settings' }, { text: 'Chat bubbles' }]), -1);
    assert.equal(chatMenuItemIndex([{ tid: 'chat-settings', text: 'Settings' }]), -1);
});

test('a menu without a chat item answers -1', () => {
    assert.equal(chatMenuItemIndex([]), -1);
    assert.equal(chatMenuItemIndex([{ text: 'Raise hand' }, { text: 'Turn on live captions' }, { text: 'Leave' }]), -1);
});

// ─── The chat watch ──────────────────────────────────────────────────────────

const healthy: ChatWatchState = { paneOpen: true, readerInstalled: true, busy: false, failures: 0, msSinceLastAttempt: 1e9 };

test('an open pane with its reader needs nothing', () => {
    assert.equal(chatWatchAction(healthy), 'none');
});

test('a closed pane is reopened', () => {
    assert.equal(chatWatchAction({ ...healthy, paneOpen: false }), 'reopen-chat');
});

test('a closed pane is left alone while the People pane is up for a screenshot or a post is opening it', () => {
    assert.equal(chatWatchAction({ ...healthy, paneOpen: false, busy: true }), 'none');
});

test('a reader removed by a reload is installed again, before anything else', () => {
    assert.equal(chatWatchAction({ ...healthy, readerInstalled: false }), 'reinstall-reader');
    assert.equal(chatWatchAction({ ...healthy, readerInstalled: false, paneOpen: false, busy: true }), 'reinstall-reader');
});

test('reopening backs off while the pane will not open, up to a ceiling', () => {
    assert.equal(chatReopenBackoffMs(0), 0);
    assert.equal(chatReopenBackoffMs(1), TEAMS_CHAT_WATCH_MS);
    assert.equal(chatReopenBackoffMs(2), 2 * TEAMS_CHAT_WATCH_MS);
    assert.equal(chatReopenBackoffMs(30), TEAMS_CHAT_REOPEN_MAX_BACKOFF_MS);
    const closed = { ...healthy, paneOpen: false, failures: 3 };
    assert.equal(chatWatchAction({ ...closed, msSinceLastAttempt: 4 * TEAMS_CHAT_WATCH_MS - 1 }), 'none');
    assert.equal(chatWatchAction({ ...closed, msSinceLastAttempt: 4 * TEAMS_CHAT_WATCH_MS }), 'reopen-chat');
});

test('teams.ts reopens the chat through the "More" menu, after the People-pane screenshot and from the chat watch', () => {
    const source = readFileSync(new URL('../src/teams.ts', import.meta.url), 'utf8');
    const open = source.slice(source.indexOf('private async openChatPanelNow('), source.indexOf('private async waitForChatInput('));
    assert.ok(open.includes('this.openChatFromMoreMenu(page)'), 'openChatPanel falls back to the "More" menu');
    assert.match(source, /reopenChat: async \(\) => \{\s+const open = await this\.openChatPanel\(page\);/);
    assert.ok(source.includes('chatWatchAction({'), 'the chat watch runs');
    assert.ok(source.includes('await this.retryFailedMessages(page);'), 'failed messages are retried before posting');
});
