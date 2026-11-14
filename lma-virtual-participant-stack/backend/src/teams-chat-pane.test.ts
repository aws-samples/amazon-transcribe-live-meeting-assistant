/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Keeping the chat pane open on the Teams browser join (teams-chat-pane.ts):
 * which toolbar control may be clicked to show the chat, and what the
 * periodic chat check does. The clicks themselves need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { AsyncLock, chatWatchAction, ChatWatchState, isChatPaneButton, TEAMS_FAILED_MESSAGE_RETRY_SELECTOR } from './teams-chat-pane.js';

const teamsSource = (): string => readFileSync(new URL('../src/teams.ts', import.meta.url), 'utf8');

test('a toolbar chat button is accepted', () => {
    assert.equal(isChatPaneButton({ aria: 'Chat' }), true);
    assert.equal(isChatPaneButton({ aria: 'Show conversation' }), true);
    assert.equal(isChatPaneButton({ text: 'Meeting chat' }), true);
    assert.equal(TEAMS_FAILED_MESSAGE_RETRY_SELECTOR, '[data-tid="message-failed-retry"]');
});

test('"Show chat bubbles" and other toggles or settings are never taken for the chat button', () => {
    assert.equal(isChatPaneButton({ aria: 'Show chat bubbles' }), false);
    assert.equal(isChatPaneButton({ text: 'Chat bubbles' }), false);
    assert.equal(isChatPaneButton({ aria: 'Hide chat' }), false);
    assert.equal(isChatPaneButton({ aria: 'Close chat' }), false);
    assert.equal(isChatPaneButton({ title: 'Chat settings' }), false);
});

test('no item of an open menu is taken for the chat button, whatever its label', () => {
    assert.equal(isChatPaneButton({ aria: 'Chat', role: 'menuitem' }), false);
    assert.equal(isChatPaneButton({ aria: 'Chat', role: 'menuitemcheckbox' }), false);
    assert.equal(isChatPaneButton({ aria: 'Chat', inMenu: true }), false);
});

// ─── The chat watch ──────────────────────────────────────────────────────────

const healthy: ChatWatchState = { paneOpen: true, readerInstalled: true, closedReported: false };

test('an open pane with its reader needs nothing', () => {
    assert.equal(chatWatchAction(healthy), 'none');
});

test('a closed pane is reported once, and again only after it has been seen open', () => {
    assert.equal(chatWatchAction({ ...healthy, paneOpen: false }), 'report-closed');
    assert.equal(chatWatchAction({ ...healthy, paneOpen: false, closedReported: true }), 'none');
    assert.equal(chatWatchAction({ ...healthy, paneOpen: true, closedReported: true }), 'report-reopened');
});

test('a reader removed by a reload is installed again, before anything else', () => {
    assert.equal(chatWatchAction({ ...healthy, readerInstalled: false }), 'reinstall-reader');
    assert.equal(chatWatchAction({ ...healthy, readerInstalled: false, paneOpen: false, closedReported: true }), 'reinstall-reader');
});

// ─── One user of the chat pane at a time ─────────────────────────────────────

const tick = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

test('work under the lock runs one at a time, in the order it asked', async () => {
    const lock = new AsyncLock();
    const order: string[] = [];
    let releaseScreenshot: () => void = () => undefined;
    const screenshot = lock.run(async () => {
        order.push('screenshot:start');
        await new Promise<void>((resolve) => { releaseScreenshot = resolve; });
        order.push('screenshot:end');
    });
    const firstPost = lock.run(async () => { order.push('post 1'); return true; });
    const secondPost = lock.run(async () => { order.push('post 2'); return true; });
    await tick();
    assert.deepEqual(order, ['screenshot:start'], 'a post waits for the screenshot to finish');
    releaseScreenshot();
    assert.equal(await firstPost, true);
    assert.equal(await secondPost, true);
    await screenshot;
    assert.deepEqual(order, ['screenshot:start', 'screenshot:end', 'post 1', 'post 2']);
    assert.equal(lock.busy, false);
});

test('the chat watch skips its turn while a post holds the lock, and runs once it is free', async () => {
    const lock = new AsyncLock();
    let finishPost: () => void = () => undefined;
    const post = lock.run(() => new Promise<void>((resolve) => { finishPost = resolve; }));
    let watched = 0;
    assert.equal(await lock.runIfFree(async () => { watched += 1; return 'checked'; }, 'skipped'), 'skipped');
    assert.equal(watched, 0);
    finishPost();
    await post;
    assert.equal(await lock.runIfFree(async () => { watched += 1; return 'checked'; }, 'skipped'), 'checked');
    assert.equal(watched, 1);
});

test('a post waits for a chat-watch check in progress', async () => {
    const lock = new AsyncLock();
    const order: string[] = [];
    let finishCheck: () => void = () => undefined;
    const check = lock.runIfFree(async () => {
        order.push('check:start');
        await new Promise<void>((resolve) => { finishCheck = resolve; });
        order.push('check:end');
    }, undefined);
    const post = lock.run(async () => { order.push('post'); });
    await tick();
    assert.deepEqual(order, ['check:start']);
    finishCheck();
    await Promise.all([check, post]);
    assert.deepEqual(order, ['check:start', 'check:end', 'post']);
});

test('the lock is released when the work under it throws', async () => {
    const lock = new AsyncLock();
    await assert.rejects(lock.run(async () => { throw new Error('page closed'); }), /page closed/);
    assert.equal(lock.busy, false);
    assert.equal(await lock.run(async () => 'next'), 'next');
    const release = await lock.acquire();
    release();
    release();
    assert.equal(lock.busy, false, 'a second release does not hand the lock to nobody');
});

// ─── What teams.ts does with them ────────────────────────────────────────────

test('teams.ts runs posts, the consent screenshots and the chat watch under one lock', () => {
    const source = teamsSource();
    assert.equal((source.match(/new AsyncLock\(\)/g) ?? []).length, 1);
    assert.match(source, /private async sendMessages\(page: Page, messages: string\[\]\): Promise<boolean> \{\s+\/\/[^\n]*\n\s+return this\.chatLock\.run\(\(\) => this\.sendMessagesNow\(page, messages\)\);/);
    assert.match(source, /screenshots: \(\) => this\.chatLock\.run\(\(\) => captureMeetingPanels\(page, \{/);
    assert.match(source, /await this\.chatLock\.runIfFree\(async \(\) => \{\s+const readerInstalled/);
    // Nothing that runs under the lock takes it again (that would wait forever).
    const now = source.slice(source.indexOf('private async sendMessagesNow('), source.indexOf('private async clickSendButton('));
    const restore = source.slice(source.indexOf('private async restoreChat('), source.indexOf('private async chatPaneButton('));
    for (const body of [now, restore]) {
        assert.ok(body.length > 0);
        assert.ok(!body.includes('chatLock') && !body.includes('this.sendMessages('), 'work under the lock takes it again');
    }
});


test('teams.ts never opens the "More" menu or clicks a menu item', () => {
    const source = teamsSource();
    for (const gone of ['callingButtons-showMoreBtn', 'openChatFromMoreMenu', 'role="menuitem"', 'chatMenuItemIndex', 'teams-chat-menu']) {
        assert.ok(!source.includes(gone), `teams.ts still refers to ${gone}`);
    }
    assert.ok(source.includes('if (isChatPaneButton(info)) return { element: handle, selector };'), 'toolbar chat buttons are filtered');
});

test('the chat watch clicks nothing and dumps the side rail at most once', () => {
    const source = teamsSource();
    const watch = source.slice(source.indexOf('const watchChat = async'), source.indexOf('setTimeout(() => { void watchChat(); }, TEAMS_CHAT_WATCH_MS);\n        } catch'));
    assert.ok(watch.length > 0, 'the chat watch is not found');
    assert.ok(!/click|restoreChat|keyboard/i.test(watch), 'the chat watch clicks or opens something');
    assert.match(watch, /if \(!sideRailDumped\) \{\s+sideRailDumped = true;\s+await this\.dumpSideRail\(page\);/);
});

test('the consent evidence on the Teams browser join does not open the People pane', () => {
    const source = teamsSource();
    const consent = source.slice(source.indexOf('attachMeetingConsent(page, {'), source.indexOf('leave: (objector) =>'));
    assert.match(consent, /showParticipants: async \(\) => \{\s+log\.info\('\[consent\] no participant-list screenshot on the Teams browser join/);
    assert.match(consent, /return false;/);
    assert.ok(!source.includes('toolbarPanels('), 'the People pane is opened for a screenshot');
});

test('a post with no compose box on screen reports that it was not posted', () => {
    const source = teamsSource();
    assert.match(source, /if \(!\(await this\.restoreChat\(page\)\)\) \{\s+log\.warn\(`\[teams-chat\] the chat compose box is not on screen; \$\{messages\.length\} message\(s\) not posted\.`\);\s+return false;/);
    assert.ok(source.includes('await this.markFailedMessagesSeen(page);'), 'earlier failures are set aside before posting');
    assert.ok(!source.includes('retryFailedMessages'), 'Teams Retry is not pressed, so a failed notice is not sent twice');
});
