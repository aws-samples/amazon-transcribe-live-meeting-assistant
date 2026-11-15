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
import {
    AsyncLock,
    chatWatchAction,
    ChatWatchState,
    composerHoldsMessage,
    isChatPaneButton,
    isComposeBox,
    isSendButton,
    TEAMS_FAILED_MESSAGE_RETRY_SELECTOR,
    CHATLESS_REMOVAL_LINE,
    CHAT_COPY_WINDOW_MS,
    ChatCopyMerger,
    TEAMS_FAILED_MARKER_SETTLE_MS,
    introForChatlessClient,
    isLightMeetingsUrl,
} from './teams-chat-pane.js';

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

// ─── Posting ─────────────────────────────────────────────────────────────────

const notice = 'This meeting is being recorded and transcribed by LMA.';

test('an empty compose box does not hold the message', () => {
    assert.equal(composerHoldsMessage('', notice), false);
    assert.equal(composerHoldsMessage('   \n ', notice), false);
});

test('a compose box holding the start of the message holds it', () => {
    assert.equal(composerHoldsMessage(notice, notice), true);
    assert.equal(composerHoldsMessage(notice.slice(0, 25), notice), true);
    // innerText breaks lines and doubles spaces where the message did not.
    assert.equal(composerHoldsMessage('This meeting  is\nbeing recorded', 'This meeting is being recorded'), true);
});

test('a compose box holding other text, or too little of the message, does not hold it', () => {
    assert.equal(composerHoldsMessage('Reply "LMA, I consent"', notice), false);
    assert.equal(composerHoldsMessage('This meeting', notice), false);
});

test('a blank message is never held', () => {
    assert.equal(composerHoldsMessage('anything', '   '), false);
});

test('only editable text is typed into, and only a Send button is clicked from a resolver guess', () => {
    assert.equal(isComposeBox({ editable: true }), true);
    assert.equal(isComposeBox({ role: 'textbox' }), true);
    assert.equal(isComposeBox({ editable: false, aria: 'View' }), false);
    assert.equal(isComposeBox({}), false);
    assert.equal(isSendButton({ aria: 'Send' }), true);
    assert.equal(isSendButton({ tid: 'newMessageCommands-send' }), true);
    assert.equal(isSendButton({ aria: 'View' }), false);
    assert.equal(isSendButton({ aria: 'More' }), false);
    assert.equal(isSendButton({ aria: 'Send', role: 'menuitem' }), false);
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
    const send = source.slice(source.indexOf('private async sendMessages('), source.indexOf('private async sendMessagesNow('));
    assert.match(send, /return this\.chatLock\.run\(\(\) => this\.sendMessagesNow\(page, messages\)\);/);
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

test('a message that did not reach the compose box is not submitted, and the batch is not posted', () => {
    const source = teamsSource();
    const now = source.slice(source.indexOf('private async sendMessagesNow('), source.indexOf('private async clickSendButton('));
    const check = now.indexOf('const ok = composerHoldsMessage(typed, message);');
    const refuse = now.indexOf('if (!ok) {', check);
    const submit = now.indexOf('await this.clickSendButton(page);');
    assert.ok(check > 0 && refuse > check && submit > refuse, 'the content check comes before the submit');
    const block = now.slice(refuse, submit);
    assert.match(block, /log\.warn\(/);
    assert.match(block, /return false;\n\s+\}/);
    assert.ok(!block.includes('clickSendButton') && !block.includes("'Enter'"), 'an empty compose box is submitted');
});

test('keystrokes and Enter go only to the focused compose box', () => {
    const source = teamsSource();
    const now = source.slice(source.indexOf('private async sendMessagesNow('), source.indexOf('private async clickSendButton('));
    assert.ok(!now.includes("page.keyboard.press('Enter')"), 'Enter is pressed without a focus check');
    assert.match(now, /if \(await this\.composerFocused\(found\.element\)\) \{\s+log\.info\(`Paste insert did not land[^\n]*\n\s+await page\.keyboard\.type\(message/);
    assert.match(source, /if \(await this\.composerFocused\(element\)\) \{\s+await page\.keyboard\.press\('Enter'\);/);
});

test('the light-meetings join does not open the View panel, which would replace the chat', () => {
    const source = readFileSync(new URL('../src/teams.ts', import.meta.url), 'utf8');
    const at = source.indexOf('Opening view panel (best-effort).');
    assert.ok(at > 0);
    const guard = source.lastIndexOf('/light-meetings/i.test(page.url())', at);
    assert.ok(guard > 0 && at - guard < 600, 'the View panel step is skipped on light-meetings');
});

// ─── A chat that Teams turns off ─────────────────────────────────────────────

const DEFAULT_INTRO =
    'Hello. I am an AI Live Meeting Assistant (LMA). I was invited by Ana Silva to join this call. ' +
    'Type "LMA leave" in chat to remove me, or "LMA help" to see the other commands. ' +
    'If there is more than one LMA here, add my name: "LMA ana leave". ' +
    'To learn more please visit: https://amazon.com/live-meeting-assistant.';

test('the light-meetings introduction gives no chat commands, and says how the VP can be removed', () => {
    assert.deepEqual(introForChatlessClient([DEFAULT_INTRO]), [
        'Hello. I am an AI Live Meeting Assistant (LMA). I was invited by Ana Silva to join this call. ' +
            `${CHATLESS_REMOVAL_LINE} To learn more please visit: https://amazon.com/live-meeting-assistant.`,
    ]);
});

test('an introduction with no chat command is posted as written on the light-meetings client', () => {
    const intro = ['Hello from LMA.  I take notes for Ana.'];
    assert.deepEqual(introForChatlessClient(intro), intro);
});

test('a message that is only a chat command is left out; the removal line is said once', () => {
    assert.deepEqual(introForChatlessClient(['Hello, I am LMA.', 'Type "LMA help" in chat.', 'Or "@LMA leave".', "I'll post a summary in the chat at the end."]), [
        'Hello, I am LMA.',
        CHATLESS_REMOVAL_LINE,
        "I'll post a summary in the chat at the end.",
    ]);
});

test('a quoted "LMA" that gives no command is kept in the light-meetings introduction', () => {
    const intro = ['I\'m "LMA", a note-taker for Ana.'];
    assert.deepEqual(introForChatlessClient(intro), intro);
});

test('the light-meetings client is told apart by its URL', () => {
    assert.equal(isLightMeetingsUrl('https://teams.microsoft.com/light-meetings/launch?lightExperience=true'), true);
    assert.equal(isLightMeetingsUrl('https://teams.microsoft.com/v2/?meetingjoin=true'), false);
    assert.equal(isLightMeetingsUrl('https://teams.live.com/light-meetings-x/'), false);
});

test('teams.ts starts the consent flow right after the introduction, and posts the chatless introduction on light-meetings', () => {
    const source = teamsSource();
    const intro = source.indexOf('introForChatlessClient(details.introMessages)');
    const consent = source.indexOf('attachMeetingConsent(page, {');
    const viewPanel = source.indexOf('Opening view panel (best-effort).');
    assert.ok(intro > 0 && consent > intro && viewPanel > consent, 'introduction, then consent, then the rest of the in-meeting setup');
    assert.match(source.slice(consent, consent + 1500), /chatMayClose: isLightMeetingsUrl\(page\.url\(\)\)/);
    assert.match(source.slice(consent, consent + 1500), /chatTurnedOff: \(\) => this\.chatTurnedOff/);
});

test('teams.ts posts nothing once the chat is shown as turned off, and submits an empty readback only on light-meetings', () => {
    const source = teamsSource();
    const send = source.slice(source.indexOf('private async sendMessagesNow('), source.indexOf('private async chatShownTurnedOff('));
    assert.match(send, /if \(this\.chatTurnedOff \|\| \(await this\.chatShownTurnedOff\(page\)\)\)/);
    assert.match(send, /!ok && typed\.length === 0 && emptyReadbackPosts/);
    assert.match(send, /const emptyReadbackPosts = isLightMeetingsUrl\(page\.url\(\)\)/);
    assert.match(source, /if \(!this\.chatTurnedOff && \(await this\.chatShownTurnedOff\(page\)\)\) void meetingConsent\.noteChatTurnedOff\(\);/, 'the chat check looks for a chat turned off');
    assert.match(send, /const pasteUnseen = pasted && afterPaste\.length === 0 && emptyReadbackPosts;\s+if \(pasteUnseen\)/, 'a pasted message is not typed again where it cannot be read back');
});

test('a Retry button right after a post is given time to go away before the post counts as failed', () => {
    const source = teamsSource();
    const send = source.slice(source.indexOf('private async sendMessagesNow('), source.indexOf('private async chatShownTurnedOff('));
    assert.match(
        send,
        /let failedNow = await this\.newFailedMessageCount\(page\);\s+if \(failedNow > 0\) \{[\s\S]*?setTimeout\(resolve, TEAMS_FAILED_MARKER_SETTLE_MS\)\);\s+failedNow = await this\.newFailedMessageCount\(page\);/,
    );
    assert.ok(TEAMS_FAILED_MARKER_SETTLE_MS >= 3000);
});

// ─── Reading the chat ────────────────────────────────────────────────────────

const read = (text: string, author: string | null = null) => ({ text, id: null, author, time: null, links: [] as string[] });

test('a message and the heading that repeats it with its sender are one message, under that sender', () => {
    const merger = new ChatCopyMerger(() => 1000);
    assert.deepEqual(merger.merge([read('Great evening!'), read('Great evening! by Strahan, Bob', 'Strahan, Bob')]), [
        read('Great evening!', 'Strahan, Bob'),
    ]);
    assert.deepEqual(merger.merge([read('testing again by Strahan, Bob', 'Strahan, Bob'), read('testing again')]), [read('testing again', 'Strahan, Bob')]);
});

test('a heading read after its message, or before it, does not make a second message', () => {
    let now = 1000;
    const merger = new ChatCopyMerger(() => now);
    assert.deepEqual(merger.merge([read('Hello everyone')]), [read('Hello everyone')]);
    now += 2000;
    assert.deepEqual(merger.merge([read('Hello everyone by Strahan, Bob', 'Strahan, Bob')]), []);
    now += 2000;
    assert.deepEqual(merger.merge([read('See you by Ana Silva', 'Ana Silva')]), [read('See you', 'Ana Silva')]);
    now += 2000;
    assert.deepEqual(merger.merge([read('See you')]), []);
});

test('a message that only ends in "by" a name, and two people sending the same words, are kept', () => {
    let now = 1000;
    const merger = new ChatCopyMerger(() => now);
    assert.deepEqual(merger.merge([read('The report is due by Friday')]), [read('The report is due by Friday')]);
    assert.deepEqual(merger.merge([read('ok')]), [read('ok')]);
    now += 1000;
    assert.deepEqual(merger.merge([read('ok')]), [read('ok')]);
    now += CHAT_COPY_WINDOW_MS + 1;
    assert.deepEqual(merger.merge([read('Hello everyone by Bo', 'Bo')]), [read('Hello everyone', 'Bo')], 'outside the window it is a message of its own');
});

test('a sender is never taken from the text of a message', () => {
    const merger = new ChatCopyMerger(() => 1000);
    assert.deepEqual(merger.merge([read('hello'), read('hello by Strahan, Bob')]), [read('hello'), read('hello by Strahan, Bob')]);
    assert.deepEqual(merger.merge([read('hi by Ana', 'Bo')]), [read('hi by Ana', 'Bo')], 'a row author that the text does not end in is not a heading');
});

test('teams.ts reads the chat through ChatCopyMerger', () => {
    assert.match(teamsSource(), /const fresh = chatCopies\.merge\(\s+await page\.evaluate\(\(extra\) => \(window as any\)\.__lmaScrapeMessages\(extra\), aiMessageSelector\),\s+\);/);
});
