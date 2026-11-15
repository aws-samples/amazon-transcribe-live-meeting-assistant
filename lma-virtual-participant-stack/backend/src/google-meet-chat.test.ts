/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Google Meet chat senders, read from the page as Meet draws it.
 *
 * readMeetChatInPage runs in the page, so it is run here against a small fake
 * DOM shaped like Meet's chat as captured live on 2026-11-07: a group holds a
 * header (the sender's name and the time, or only the time on the reader's own
 * messages) beside an element holding the group's messages, each marked with
 * `data-message-id`, and nothing marks the group itself.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
    MEET_CHAT_HEADER_MAX_LEVELS,
    MEET_IN_PAGE_SELECTORS,
    readMeetChatInPage,
    senderFromMeetGroupHeader,
    stripMeetMessageActions,
} from './google-meet-page.js';

type FakeNode = FakeElement | FakeText;

class FakeText {
    readonly nodeType = 3;
    parentElement: FakeElement | null = null;
    constructor(readonly textContent: string) {}
    get childNodes(): FakeNode[] {
        return [];
    }
}

class FakeElement {
    readonly nodeType = 1;
    parentElement: FakeElement | null = null;
    readonly childNodes: FakeNode[];
    constructor(
        readonly attrs: Record<string, string>,
        children: Array<FakeNode | string>,
    ) {
        this.childNodes = children.map((c) => (typeof c === 'string' ? new FakeText(c) : c));
        for (const c of this.childNodes) c.parentElement = this;
    }
    get textContent(): string {
        return this.childNodes.map((c) => c.textContent).join('');
    }
    get innerText(): string {
        return this.childNodes
            .map((c) => (c.nodeType === 3 ? c.textContent : (c as FakeElement).innerText))
            .join(' ');
    }
    getAttribute(name: string): string | null {
        return name in this.attrs ? this.attrs[name] : null;
    }
    matches(selector: string): boolean {
        const attr = /^\[([a-z-]+)\]$/.exec(selector);
        if (attr) return attr[1] in this.attrs;
        if (selector === 'a[href]') return this.attrs.tag === 'a' && 'href' in this.attrs;
        throw new Error(`fake DOM: unsupported selector ${selector}`);
    }
    closest(selector: string): FakeElement | null {
        for (let e: FakeElement | null = this; e; e = e.parentElement) if (e.matches(selector)) return e;
        return null;
    }
    querySelectorAll(selector: string): FakeElement[] {
        const found: FakeElement[] = [];
        const walk = (e: FakeElement): void => {
            for (const c of e.childNodes) {
                if (c.nodeType !== 1) continue;
                const el = c as FakeElement;
                if (el.matches(selector)) found.push(el);
                walk(el);
            }
        };
        walk(this);
        return found;
    }
    get href(): string {
        return this.attrs.href ?? '';
    }
}

const div = (children: Array<FakeNode | string>, attrs: Record<string, string> = {}): FakeElement =>
    new FakeElement(attrs, children);

/** A message row as Meet draws it: the text three wrappers deep. */
const message = (id: string, ...content: Array<FakeNode | string>): FakeElement =>
    div([div([div([div(content)])])], { 'data-message-id': id });

/** A group: header (name and time, or time only) beside the run of messages. */
const group = (header: string[], ...messages: FakeElement[]): FakeElement =>
    div([div([div([div(header.map((h) => div([h]))), div(messages)])])]);

function read(...groups: FakeElement[]) {
    const root = div(groups);
    const g = globalThis as any;
    g.window = {};
    g.document = { querySelectorAll: (s: string) => root.querySelectorAll(s) };
    try {
        return readMeetChatInPage({
            message: MEET_IN_PAGE_SELECTORS.chatMessage,
            group: MEET_IN_PAGE_SELECTORS.chatGroup,
            maxLevels: MEET_CHAT_HEADER_MAX_LEVELS,
        }).map((m) => ({ ...m, sender: senderFromMeetGroupHeader(m.header) }));
    } finally {
        delete g.window;
        delete g.document;
    }
}

test("a message is credited to the name in its group's header", () => {
    const [m] = read(group(['Conference Room 4', '11:02 PM'], message('spaces/x/messages/1', 'hello from the other seat')));
    assert.equal(m.header, 'Conference Room 4 11:02 PM');
    assert.equal(m.sender, 'Conference Room 4');
    assert.equal(m.text, 'hello from the other seat');
    assert.equal(m.id, 'spaces/x/messages/1');
});

test('every message of a run is credited to the run’s sender', () => {
    const got = read(
        group(['Priya Laptop', '10:57 PM'], message('m1', 'first'), message('m2', 'second')),
        group(['Conference Room 4', '10:58 PM'], message('m3', 'third')),
    );
    assert.deepEqual(
        got.map((m) => [m.text, m.sender]),
        [
            ['first', 'Priya Laptop'],
            ['second', 'Priya Laptop'],
            ['third', 'Conference Room 4'],
        ],
    );
});

test("the reader's own messages, headed by the time alone, have no sender", () => {
    const [m] = read(group(['10:57 PM'], message('m1', 'demo room check one')));
    assert.equal(m.header, '10:57 PM');
    assert.equal(m.sender, null);
});

test('a header is not borrowed from a neighbouring group', () => {
    // A message with no header of its own: the walk would reach text holding
    // both groups' times, which is not read as a sender.
    const orphan = div([div([message('m9', 'orphan')])]);
    const root = div([group(['Alice', '9:00 AM'], message('m1', 'a')), group(['Bob', '9:01 AM']), orphan]);
    const got = read(root);
    assert.equal(got.find((m) => m.text === 'orphan')?.sender ?? null, null);
    assert.equal(got.find((m) => m.text === 'a')?.sender, 'Alice');
});

test('a message is read once however often the list is read', () => {
    const root = div([group(['Alice', '9:00 AM'], message('m1', 'hi'))]);
    const g = globalThis as any;
    g.window = {};
    g.document = { querySelectorAll: (s: string) => root.querySelectorAll(s) };
    const sels = {
        message: MEET_IN_PAGE_SELECTORS.chatMessage,
        group: MEET_IN_PAGE_SELECTORS.chatGroup,
        maxLevels: MEET_CHAT_HEADER_MAX_LEVELS,
    };
    try {
        assert.equal(readMeetChatInPage(sels).length, 1);
        assert.equal(readMeetChatInPage(sels).length, 0);
    } finally {
        delete g.window;
        delete g.document;
    }
});

test('a group marked with data-sender-id (not seen live) is read through the mark', () => {
    const marked = div([div(['Dana', ' 8:15 AM']), div([message('m1', 'marked')])], { 'data-sender-id': 'u1' });
    const [m] = read(div([marked]));
    assert.equal(m.sender, 'Dana');
});

test("a message's text leaves out the pin button Meet draws on it", () => {
    assert.equal(
        stripMeetMessageActions('say "LMA, I consent" or "LMA, I object". keep Pin message'),
        'say "LMA, I consent" or "LMA, I object".',
    );
    assert.equal(stripMeetMessageActions('hello Unpin message'), 'hello');
    assert.equal(stripMeetMessageActions('hello  Pin message'), 'hello');
    assert.equal(stripMeetMessageActions('I object'), 'I object');
    assert.equal(stripMeetMessageActions('please keep this'), 'please keep this');
    assert.equal(stripMeetMessageActions('can you keep pin message'), 'can you keep pin message');
});
