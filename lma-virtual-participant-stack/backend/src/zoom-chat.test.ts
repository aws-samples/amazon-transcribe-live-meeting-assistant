/**
 * The Zoom browser join's meeting chat, read from the page as Zoom draws it.
 *
 * readZoomChatInPage and zoomChatPanelStateInPage run in the page, so they are
 * run here against a small fake DOM shaped like the Zoom web client's chat
 * panel (web client 7.2, November 2026): each message is a
 * `[id^="chat-item-container-"]` element whose `data-id` is Zoom's message id,
 * holding the message row `div#chat-message-content-<position>[role=row]`
 * whose aria-label is "<sender> to <recipient>, <time>, <text>". Zoom unmounts
 * the rows when its chat panel closes (it closes it itself when someone starts
 * sharing their screen) and draws new elements when it is reopened.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
    parseShownSize,
    readZoomChatInPage,
    ZOOM_CHAT_SELECTORS,
    zoomChatAttachments,
    ZoomChatPanelKeeper,
    zoomChatPanelStateInPage,
} from './zoom-chat-page.js';
import { parseZoomChatLabel, zoomChatAudience } from './zoom.js';

// ---- a fake DOM with just enough of querySelector for these selectors ----

type Attr = { name: string; op: '' | '=' | '^='; value: string; ci: boolean };
type Compound = { tag: string | null; classes: string[]; attrs: Attr[] };

function parseCompound(selector: string): Compound {
    const out: Compound = { tag: null, classes: [], attrs: [] };
    const re = /^([a-z]+)|\.([\w-]+)|\[([\w-]+)(?:(\^?=)"([^"]*)"(\s+i)?)?\]/gy;
    let m: RegExpExecArray | null;
    let at = 0;
    while (at < selector.length && (m = re.exec(selector))) {
        if (m[1]) out.tag = m[1];
        else if (m[2]) out.classes.push(m[2]);
        else out.attrs.push({ name: m[3], op: (m[4] as Attr['op']) ?? '', value: m[5] ?? '', ci: !!m[6] });
        at = re.lastIndex;
    }
    if (at !== selector.length) throw new Error(`fake DOM: unsupported selector ${selector}`);
    return out;
}

class FakeElement {
    parentElement: FakeElement | null = null;
    children: FakeElement[] = [];
    text = '';
    constructor(
        readonly tag: string,
        readonly attrs: Record<string, string>,
        children: Array<FakeElement | string> = [],
    ) {
        for (const c of children) {
            if (typeof c === 'string') this.text += c;
            else this.append(c);
        }
    }
    append(child: FakeElement): void {
        child.parentElement = this;
        this.children.push(child);
    }
    remove(): void {
        if (!this.parentElement) return;
        this.parentElement.children = this.parentElement.children.filter((c) => c !== this);
        this.parentElement = null;
    }
    get id(): string {
        return this.attrs.id ?? '';
    }
    get textContent(): string {
        return this.text + this.children.map((c) => c.textContent).join('');
    }
    get href(): string {
        return this.attrs.href ?? '';
    }
    getAttribute(name: string): string | null {
        return name in this.attrs ? this.attrs[name] : null;
    }
    setAttribute(name: string, value: string): void {
        this.attrs[name] = value;
    }
    matches(selector: string): boolean {
        const c = parseCompound(selector);
        if (c.tag && c.tag !== this.tag) return false;
        const classes = (this.attrs.class ?? '').split(/\s+/);
        if (!c.classes.every((k) => classes.includes(k))) return false;
        return c.attrs.every(({ name, op, value, ci }) => {
            const v = this.getAttribute(name);
            if (v === null) return false;
            const [have, want] = ci ? [v.toLowerCase(), value.toLowerCase()] : [v, value];
            if (op === '=') return have === want;
            if (op === '^=') return have.startsWith(want);
            return true;
        });
    }
    closest(selector: string): FakeElement | null {
        for (let e: FakeElement | null = this; e; e = e.parentElement) if (e.matches(selector)) return e;
        return null;
    }
    querySelectorAll(selector: string): FakeElement[] {
        const found: FakeElement[] = [];
        const walk = (e: FakeElement): void => {
            for (const c of e.children) {
                if (c.matches(selector)) found.push(c);
                walk(c);
            }
        };
        walk(this);
        return found;
    }
    querySelector(selector: string): FakeElement | null {
        return this.querySelectorAll(selector)[0] ?? null;
    }
}

const el = (tag: string, attrs: Record<string, string> = {}, children: Array<FakeElement | string> = []) =>
    new FakeElement(tag, attrs, children);

/** One message as Zoom draws it: the container with the message id, around the labelled row. */
function message(
    position: number,
    msgId: string | null,
    label: string,
    opts: { links?: string[]; rowId?: string | null; extra?: FakeElement[] } = {},
): FakeElement {
    const text = label.split(', ').slice(2).join(', ');
    const body = el('div', { class: 'new-chat-message__text-box' }, [
        text,
        ...(opts.links ?? []).map((href) => el('a', { href }, [href])),
        ...(opts.extra ?? []),
    ]);
    const rowAttrs: Record<string, string> = { role: 'row', 'aria-label': label };
    const rowId = opts.rowId === undefined ? `chat-message-content-${position}` : opts.rowId;
    if (rowId) rowAttrs.id = rowId;
    const row = el('div', rowAttrs, [el('div', { class: 'new-chat-message__content' }, [body])]);
    const containerAttrs: Record<string, string> = { id: `chat-item-container-${position}` };
    if (msgId) containerAttrs['data-id'] = msgId;
    return el('div', containerAttrs, [el('div', { class: 'new-chat-item__chat-info-header' }), row]);
}

/** The chat panel: the list Zoom marks "Chat Message List", inside the chat container. */
function chatPanel(...messages: FakeElement[]): { panel: FakeElement; list: FakeElement } {
    const list = el('div', { 'aria-label': 'Chat Message List', class: 'chat-virtualized-list' }, messages);
    const panel = el('div', { class: 'chat-container' }, [el('div', { class: 'chat-container__chat-list' }, [list])]);
    return { panel, list };
}

/** A page whose body is the given tree; the in-page functions run against it. */
class FakePage {
    readonly body = el('body');
    private g = globalThis as any;
    constructor() {
        this.g.window = {};
        this.g.document = {
            querySelector: (s: string) => this.body.querySelector(s),
            querySelectorAll: (s: string) => this.body.querySelectorAll(s),
        };
    }
    read(settleMs = 0, watchMs = 0) {
        return readZoomChatInPage({ ...ZOOM_CHAT_SELECTORS, settleMs, watchMs });
    }
    panelState() {
        return zoomChatPanelStateInPage(ZOOM_CHAT_SELECTORS);
    }
    close(): void {
        delete this.g.window;
        delete this.g.document;
    }
}

function withPage(fn: (page: FakePage) => void): void {
    const page = new FakePage();
    try {
        fn(page);
    } finally {
        page.close();
    }
}

const INTRO = 'You to Everyone, 4:01 PM, Hello, I am an AI assistant. Type "LMA help" to see the commands.';

// ---- reading the chat ----

test('a chat message typed by another participant on Zoom is read with its sender', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        assert.deepEqual(page.read().rows, [], 'the rows on screen when listening starts are the baseline');

        list.append(message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA help'));
        const { rows } = page.read();
        assert.equal(rows.length, 1);
        assert.equal(rows[0].id, 'm-1');
        const parsed = parseZoomChatLabel(rows[0].label);
        assert.equal(parsed.sender, 'Alice Example');
        assert.equal(parsed.body, 'LMA help');
        assert.equal(zoomChatAudience(parsed.recipient), 'everyone');
    });
});

test("the VP's own chat messages are not read as commands", () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        list.append(message(1, 'm-own', 'You to Everyone, 4:15 PM, LMA leave'));
        list.append(message(2, 'm-own-2', 'you to Meeting Group Chat, 4:15 PM, LMA pause'));
        assert.deepEqual(page.read().rows, []);
    });
});

test('messages already in the chat when the VP starts listening are not read, a Team Chat group chat’s history included', () => {
    withPage((page) => {
        const { panel } = chatPanel(
            message(0, 'old-1', 'Alice Example to Meeting Group Chat, 9:02 AM, LMA leave'),
            message(1, 'm-intro', INTRO),
        );
        page.body.append(panel);
        const first = page.read();
        assert.equal(first.baseline, true);
        assert.deepEqual(first.rows, []);
        assert.deepEqual(page.read().rows, [], 'and are not read later either');
    });
});

test('rows drawn within the settle window after the list first appears are taken as already there', () => {
    withPage((page) => {
        const { panel, list } = chatPanel();
        page.body.append(panel);
        page.read(60_000);
        list.append(message(0, 'late-history', 'Alice Example to Meeting Group Chat, 9:02 AM, LMA leave'));
        const second = page.read(60_000);
        assert.equal(second.baseline, true);
        assert.deepEqual(second.rows, []);
    });
});

test('a message sent while Zoom had closed the chat panel is read when the panel is reopened, and earlier ones are not read again', () => {
    withPage((page) => {
        const first = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(first.panel);
        page.read();
        first.list.append(message(1, 'm-1', 'Alice Example to Everyone, 4:05 PM, hello all'));
        assert.equal(page.read().rows.length, 1);

        // Screen share starts: Zoom closes the panel and its rows go.
        first.panel.remove();
        const closed = page.read();
        assert.equal(closed.listFound, false);
        assert.deepEqual(closed.rows, []);

        // Reopened: Zoom draws new elements for every row, old and new.
        const again = chatPanel(
            message(0, 'm-intro', INTRO),
            message(1, 'm-1', 'Alice Example to Everyone, 4:05 PM, hello all'),
            message(2, 'm-2', 'Bo Chen to Everyone, 4:14 PM, LMA help'),
        );
        page.body.append(again.panel);
        const { rows } = page.read();
        assert.deepEqual(
            rows.map((r) => r.id),
            ['m-2'],
        );
        assert.equal(parseZoomChatLabel(rows[0].label).sender, 'Bo Chen');
    });
});

test('a message is read once however often Zoom draws its row again', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        const row = message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA pause');
        list.append(row);
        assert.equal(page.read().rows.length, 1);
        row.remove();
        list.append(message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA pause'));
        assert.equal(page.read().rows.length, 0);
    });
});

test('the same command typed twice is read twice', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        list.append(message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA help'));
        list.append(message(2, 'm-2', 'Alice Example to Everyone, 4:14 PM, LMA help'));
        assert.deepEqual(
            page.read().rows.map((r) => r.id),
            ['m-1', 'm-2'],
        );
    });
});

test('a row without a message id that reuses a position is read as the new message it now holds', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, null, INTRO));
        page.body.append(panel);
        page.read();
        list.append(message(1, null, 'Alice Example to Everyone, 4:14 PM, first'));
        assert.equal(page.read().rows[0].id, '1', 'the row position stands in for the id');
        list.children[1].remove();
        list.append(message(1, null, 'Bo Chen to Everyone, 4:15 PM, LMA help'));
        const { rows } = page.read();
        assert.equal(rows.length, 1);
        assert.equal(parseZoomChatLabel(rows[0].label).body, 'LMA help');
    });
});

test('a row is not read before Zoom fills in its label, and is read once it has one', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        const m = message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA help');
        const row = m.querySelector('[role="row"]') as FakeElement;
        row.setAttribute('aria-label', '');
        list.append(m);
        assert.deepEqual(page.read().rows, []);
        row.setAttribute('aria-label', 'Alice Example to Everyone, 4:14 PM, LMA help');
        assert.equal(page.read().rows.length, 1);
    });
});

test("a message's links are read with it", () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        list.append(
            message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, see https://example.com/doc', {
                links: ['https://example.com/doc'],
            }),
        );
        assert.deepEqual(page.read().rows[0].links, ['https://example.com/doc']);
    });
});

test('message rows Zoom no longer marks with the chat-message-content id are read as labelled grid rows', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO, { rowId: null }));
        page.body.append(panel);
        page.read();
        list.append(message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA help', { rowId: null }));
        const { rows } = page.read();
        assert.equal(rows.length, 1);
        assert.equal(rows[0].id, 'm-1');
    });
});

test('a message list Zoom no longer labels is found from the chat container', () => {
    withPage((page) => {
        const list = el('div', { class: 'chat-virtualized-list' }, [message(0, 'm-intro', INTRO)]);
        page.body.append(el('div', { class: 'chat-container' }, [el('div', { class: 'chat-container__chat-list' }, [list])]));
        assert.equal(page.read().listFound, true);
        list.append(message(1, 'm-1', 'Alice Example to Everyone, 4:14 PM, LMA help'));
        assert.equal(page.read().rows.length, 1);
    });
});

// ---- the recipient ----

test('a message to Meeting Group Chat is read as sent to everyone', () => {
    const parsed = parseZoomChatLabel('Alice Example to Meeting Group Chat, 11:14 AM, LMA help');
    assert.deepEqual(parsed, {
        sender: 'Alice Example',
        recipient: 'Meeting Group Chat',
        body: 'LMA help',
        time: '11:14 AM',
    });
    assert.equal(zoomChatAudience(parsed.recipient), 'everyone');
    assert.equal(zoomChatAudience('Everyone'), 'everyone');
    assert.equal(zoomChatAudience('You (Direct Message)'), 'direct');
    assert.equal(zoomChatAudience('Bo Chen'), 'direct');
});

// ---- keeping the panel open ----

test("the chat panel's state is read from the toolbar button, unread count and all", () => {
    withPage((page) => {
        const button = el('button', { 'aria-label': 'open the chat panel 2 unread messages' });
        page.body.append(button);
        assert.equal(page.panelState(), 'closed');
        button.setAttribute('aria-label', 'Close the chat panel');
        assert.equal(page.panelState(), 'open');
        button.setAttribute('aria-label', 'Participants');
        assert.equal(page.panelState(), 'unknown');
    });
});

test('the chat panel is reopened only when the toolbar shows it closed', () => {
    const keeper = new ZoomChatPanelKeeper(5_000, 60_000);
    assert.equal(keeper.shouldReopen('open', 0), false);
    assert.equal(keeper.shouldReopen('unknown', 0), false, 'never clicked when the panel may be open');
    assert.equal(keeper.shouldReopen('closed', 1_000), true);
});

test('reopening the chat panel is spaced out while it stays closed, and back to quick once it is open', () => {
    const keeper = new ZoomChatPanelKeeper(5_000, 20_000);
    keeper.attempted(0, false);
    assert.equal(keeper.shouldReopen('closed', 9_000), false, 'the spacing doubles after a failed attempt');
    assert.equal(keeper.shouldReopen('closed', 10_000), true);
    keeper.attempted(10_000, false);
    keeper.attempted(30_000, false);
    assert.equal(keeper.currentGapMs, 20_000, 'up to the cap');
    assert.equal(keeper.shouldReopen('open', 31_000), false);
    assert.equal(keeper.currentGapMs, 5_000);
    assert.equal(keeper.shouldReopen('closed', 36_000), true);
    keeper.attempted(36_000, true);
    assert.equal(keeper.shouldReopen('closed', 40_000), false);
    assert.equal(keeper.shouldReopen('closed', 41_000), true);
});

// ---- attachments ----

/** Zoom's emoji reaction icons, which every row holds as images. */
const reactions = () =>
    el('div', { class: 'chat-reaction-picker' }, [
        el('img', { class: 'emoji-icon', src: 'https://zoom.us/emoji/1f44d.png', alt: 'thumbs up' }),
        el('img', { class: 'emoji-icon', src: 'https://zoom.us/emoji/2764.png', alt: 'heart' }),
    ]);

/** A shared file's card as Zoom draws it: name, size, "Click to download", no link. */
const fileCard = (name: string, size: string) =>
    el('div', { class: 'chat-file-item', 'aria-label': `${name}, ${size}, Click to download` }, [
        el('span', { class: 'chat-file-item__name' }, [name]),
        el('span', { class: 'chat-file-item__size' }, [size]),
        el('span', {}, ['Click to download']),
    ]);

test("a Zoom message's pasted image and file card are read with it, and the emoji reaction icons are not", () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        list.append(
            message(1, 'm-img', 'Alice Example to Everyone, 4:20 PM, whiteboard.png', {
                extra: [el('img', { class: 'chat-image-preview-img', src: 'blob:https://app.zoom.us/1234', alt: 'whiteboard.png' }), reactions()],
            }),
        );
        list.append(
            message(2, 'm-file', 'Alice Example to Everyone, 4:21 PM, Q3 plan.pdf', {
                extra: [fileCard('Q3 plan.pdf', '199.6 KB'), reactions()],
            }),
        );
        list.append(message(3, 'm-text', 'Alice Example to Everyone, 4:22 PM, thanks', { extra: [reactions()] }));
        const { rows } = page.read();
        assert.equal(rows.length, 3);
        assert.deepEqual(rows[0].attachments, [{ kind: 'image', name: 'whiteboard.png', src: 'blob:https://app.zoom.us/1234', index: 0 }]);
        assert.deepEqual(rows[1].attachments, [{ kind: 'document', name: 'Q3 plan.pdf', sizeText: '199.6 KB', index: 0 }]);
        assert.equal(rows[1].messageId, 'm-file');
        assert.equal(rows[2].attachments, undefined, 'a row with only reaction icons has no attachments');
    });
});

test('a Zoom image is fetched in the page and a file card by clicking it, through its message id', async () => {
    const blobs: string[] = [];
    const cards: Array<[string, number]> = [];
    const fetchers = {
        blob: (src: string) => {
            blobs.push(src);
            return async () => ({ ok: false as const, reason: 'unavailable' as const });
        },
        fileCard: (messageId: string, index: number) => {
            cards.push([messageId, index]);
            return async () => ({ ok: false as const, reason: 'unavailable' as const });
        },
    };
    const built = zoomChatAttachments(
        {
            label: 'x',
            id: 'm-1',
            messageId: 'm-1',
            links: [],
            attachments: [
                { kind: 'image', name: 'a.png', src: 'blob:https://app.zoom.us/1', index: 0 },
                { kind: 'image', name: 'b.png', src: 'https://zoom.us/x.png', index: 1 },
                { kind: 'document', name: 'c.pdf', sizeText: '1.5 MB', index: 0 },
            ],
        },
        fetchers,
    );
    assert.equal(typeof built[0].fetch, 'function');
    assert.deepEqual(await built[1].fetch?.(1000), { ok: false, reason: 'unavailable' }, 'only a blob: address is fetched');
    assert.equal(typeof built[2].fetch, 'function');
    assert.equal(built[2].size, 1_572_864);
    assert.deepEqual(blobs, ['blob:https://app.zoom.us/1']);
    assert.deepEqual(cards, [['m-1', 0]]);

    const noId = zoomChatAttachments(
        { label: 'x', id: 'row-3', messageId: null, links: [], attachments: [{ kind: 'document', name: 'c.pdf', index: 0 }] },
        fetchers,
    );
    assert.equal(noId[0].fetch, undefined, 'a card is clicked only through a message id that names its container');
    const odd = zoomChatAttachments(
        { label: 'x', id: 'a"b', messageId: 'a"] , body [x="', links: [], attachments: [{ kind: 'document', name: 'c.pdf', index: 0 }] },
        fetchers,
    );
    assert.equal(odd[0].fetch, undefined, 'nor through one that is not a plain id');
});

test('the size Zoom shows on a file card is read in bytes', () => {
    assert.equal(parseShownSize('204 KB'), 208_896);
    assert.equal(parseShownSize('16 bytes'), 16);
    assert.equal(parseShownSize('2 GB'), 2 * 1024 ** 3);
    assert.equal(parseShownSize('Click to download'), null);
    assert.equal(parseShownSize(null), null);
});

// ---- attachments that appear after the row was read ----

/** Runs `fn` with Date.now returning what `clock.now` holds. */
function withClock(fn: (clock: { now: number }) => void): void {
    const real = Date.now;
    const clock = { now: 1_000_000 };
    Date.now = () => clock.now;
    try {
        fn(clock);
    } finally {
        Date.now = real;
    }
}

test('an image pasted after its text is read once it has its blob: address, as a late attachment of the same row', () => {
    withClock((clock) => {
        withPage((page) => {
            const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
            page.body.append(panel);
            page.read(0, 60_000);
            const preview = el('img', { class: 'chat-image-preview-img', src: '', alt: 'Screenshot.png' });
            const row = message(1, 'm-shot', 'Alice Example to Everyone, 4:20 PM, Screenshot pasted:', { extra: [reactions()] });
            list.append(row);
            const first = page.read(0, 60_000);
            assert.equal(first.rows.length, 1);
            assert.equal(first.rows[0].attachments, undefined, 'read before the image was there');

            // The image element is drawn, still uploading: not taken yet.
            row.querySelector('.new-chat-message__text-box')!.append(preview);
            clock.now += 2_000;
            assert.deepEqual(page.read(0, 60_000).late, []);

            preview.setAttribute('src', 'blob:https://app.zoom.us/shot');
            clock.now += 2_000;
            const later = page.read(0, 60_000);
            assert.deepEqual(later.rows, [], 'the row is not read again as a new message');
            assert.equal(later.late?.length, 1);
            assert.equal(later.late?.[0].messageId, 'm-shot');
            assert.deepEqual(later.late?.[0].attachments, [
                { kind: 'image', name: 'Screenshot.png', src: 'blob:https://app.zoom.us/shot', index: 0 },
            ]);

            clock.now += 2_000;
            assert.deepEqual(page.read(0, 60_000).late, [], 'and only once');
        });
    });
});

test('a row is no longer watched for attachments once its window has passed', () => {
    withClock((clock) => {
        withPage((page) => {
            const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
            page.body.append(panel);
            page.read(0, 60_000);
            const row = message(1, 'm-slow', 'Alice Example to Everyone, 4:20 PM, see below', {});
            list.append(row);
            page.read(0, 60_000);
            clock.now += 61_000;
            row.querySelector('.new-chat-message__text-box')!.append(
                el('img', { class: 'chat-image-preview-img', src: 'blob:https://app.zoom.us/late', alt: 'late.png' }),
            );
            assert.deepEqual(page.read(0, 60_000).late, []);
        });
    });
});

test('an image that never loads within the window is returned once the window closes, with no address, to be recorded by name', () => {
    withClock((clock) => {
        withPage((page) => {
            const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
            page.body.append(panel);
            page.read(0, 60_000);
            list.append(
                message(1, 'm-stuck', 'Alice Example to Everyone, 4:20 PM, here', {
                    extra: [el('img', { class: 'chat-image-preview-img', src: '', alt: 'stuck.png' })],
                }),
            );
            assert.equal(page.read(0, 60_000).rows[0].attachments, undefined);
            clock.now += 30_000;
            assert.deepEqual(page.read(0, 60_000).late, []);
            clock.now += 31_000;
            const closed = page.read(0, 60_000).late;
            assert.equal(closed?.length, 1);
            assert.equal(closed?.[0].messageId, 'm-stuck');
            assert.deepEqual(closed?.[0].attachments, [{ kind: 'image', name: 'stuck.png', src: '', index: 0 }]);
            assert.deepEqual(page.read(0, 60_000).late, [], 'and only once');
        });
    });
});

test('rows are not watched for late attachments unless asked to be', () => {
    withPage((page) => {
        const { panel, list } = chatPanel(message(0, 'm-intro', INTRO));
        page.body.append(panel);
        page.read();
        const row = message(1, 'm-x', 'Alice Example to Everyone, 4:20 PM, hi', {});
        list.append(row);
        page.read();
        row.querySelector('.new-chat-message__text-box')!.append(
            el('img', { class: 'chat-image-preview-img', src: 'blob:https://app.zoom.us/x', alt: 'x.png' }),
        );
        assert.deepEqual(page.read().late, []);
    });
});
