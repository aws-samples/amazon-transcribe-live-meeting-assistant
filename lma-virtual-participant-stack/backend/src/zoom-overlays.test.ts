/**
 * Zoom's in-meeting notices are dismissed; nothing else in the meeting is.
 *
 * dismissZoomOverlaysInPage runs in the page, so it is run here against a
 * small fake DOM shaped like what the Zoom web client (November 2026) showed
 * on the VP's screen: the "Introducing My Notes" callout with its "Got it"
 * button, and the banner advising to enable graphics/hardware acceleration
 * with an unlabelled close icon. Neither is inside a `.zm-modal`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { dismissZoomOverlaysInPage, ZOOM_OVERLAY_SPEC, zoomOverlayCheckDelayMs } from './zoom-overlays.js';

// ---- a fake DOM with just enough for the in-page function ----

class FakeElement {
    parentElement: FakeElement | null = null;
    children: FakeElement[] = [];
    ownText = '';
    clicks = 0;
    hidden = false;
    constructor(
        readonly tag: string,
        readonly attrs: Record<string, string> = {},
        children: Array<FakeElement | string> = [],
    ) {
        for (const c of children) {
            if (typeof c === 'string') this.ownText += c;
            else {
                c.parentElement = this;
                this.children.push(c);
            }
        }
    }
    get textContent(): string {
        return this.ownText + this.children.map((c) => c.textContent).join(' ');
    }
    getAttribute(name: string): string | null {
        return name in this.attrs ? this.attrs[name] : null;
    }
    getBoundingClientRect(): { width: number; height: number } {
        let hidden = false;
        for (let n: FakeElement | null = this; n; n = n.parentElement) hidden ||= n.hidden;
        return hidden ? { width: 0, height: 0 } : { width: 10, height: 10 };
    }
    click(): void {
        this.clicks++;
    }
    matches(selector: string): boolean {
        return selector.split(',').some((part) => {
            const s = part.trim();
            if (s === 'button') return this.tag === 'button';
            if (s === '[role="button"]') return this.attrs.role === 'button';
            if (s === '[class*="close" i]') return /close/i.test(this.attrs.class ?? '');
            throw new Error(`fake DOM: unsupported selector ${s}`);
        });
    }
    descendants(): FakeElement[] {
        return this.children.flatMap((c) => [c, ...c.descendants()]);
    }
    querySelectorAll(selector: string): FakeElement[] {
        return this.descendants().filter((e) => e.matches(selector));
    }
    querySelector(selector: string): FakeElement | null {
        return this.querySelectorAll(selector)[0] ?? null;
    }
}

const el = (tag: string, attrs: Record<string, string> = {}, ...children: Array<FakeElement | string>) =>
    new FakeElement(tag, attrs, children);

function withPage<T>(body: FakeElement, fn: () => T): T {
    const g = globalThis as any;
    const before = g.document;
    g.document = body;
    try {
        return fn();
    } finally {
        g.document = before;
    }
}

function myNotesCallout() {
    const gotIt = el('button', { class: 'zmu-btn zmu-btn--primary' }, 'Got it');
    const callout = el(
        'div',
        { class: 'notes-guide' },
        el('h3', {}, 'Introducing My Notes'),
        el('p', {}, 'Edit your meeting notes with the powerful editor and advanced services. ', el('a', {}, 'Learn more')),
        gotIt,
    );
    return { callout, gotIt };
}

function accelerationBanner() {
    const close = el('span', { class: 'zm-icon-close', role: 'button' });
    const banner = el(
        'div',
        { class: 'notification-bar' },
        el(
            'span',
            {},
            "For a better meeting experience, enable the option 'Use graphics/hardware acceleration' under browser system settings.",
            el('a', {}, 'Learn more'),
        ),
        close,
    );
    return { banner, close };
}

/** The meeting around the notices: a toolbar and the chat panel. */
function meeting(...notices: FakeElement[]) {
    const chatClose = el('button', { 'aria-label': 'close the chat panel' });
    const leave = el('button', {}, 'Leave');
    const chat = el(
        'div',
        { class: 'chat-container' },
        el('div', {}, 'Meeting Chat'),
        chatClose,
        el('div', { role: 'row', 'aria-label': 'Bob to Everyone, 3:02 PM, introducing my notes is new, close it' }, 'Introducing My Notes is new'),
    );
    const toolbar = el('div', { class: 'footer' }, el('button', {}, 'Mute'), el('button', {}, 'Chat'), leave);
    const body = el('body', {}, el('div', { id: 'root' }, toolbar, chat, ...notices));
    return { body, chatClose, leave };
}

test('the My Notes callout is dismissed with its "Got it" button', () => {
    const { callout, gotIt } = myNotesCallout();
    const { body } = meeting(callout);
    const done = withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC));
    assert.equal(gotIt.clicks, 1);
    assert.equal(done.length, 1);
    assert.equal(done[0].button, 'got it');
    assert.match(done[0].notice, /^introducing my notes/);
});

test('the hardware-acceleration banner is closed with its unlabelled close icon', () => {
    const { banner, close } = accelerationBanner();
    const { body } = meeting(banner);
    const done = withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC));
    assert.equal(close.clicks, 1);
    assert.deepEqual(done.map((d) => d.button), ['close icon']);
});

test('both notices on screen together are both dismissed, each once', () => {
    const { callout, gotIt } = myNotesCallout();
    const { banner, close } = accelerationBanner();
    // A labelled button holding the icon: one click for the banner, not two.
    const labelled = el('button', { 'aria-label': 'Close' }, close);
    banner.children.splice(banner.children.indexOf(close), 1, labelled);
    labelled.parentElement = banner;
    const { body } = meeting(callout, banner);
    const done = withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC));
    assert.equal(gotIt.clicks, 1);
    assert.equal(labelled.clicks + close.clicks, 1);
    assert.equal(done.length, 2);
});

test('the meeting itself is left alone, even when the chat mentions a notice', () => {
    const { body, chatClose, leave } = meeting();
    const done = withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC));
    assert.deepEqual(done, []);
    assert.equal(chatClose.clicks, 0);
    assert.equal(leave.clicks, 0);
});

test('a hidden notice is not clicked', () => {
    const { callout, gotIt } = myNotesCallout();
    callout.hidden = true;
    const { body } = meeting(callout);
    assert.deepEqual(
        withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC)),
        [],
    );
    assert.equal(gotIt.clicks, 0);
});

test('a "Got it" outside any known notice is not clicked', () => {
    const other = el('button', {}, 'Got it');
    const { body } = meeting(el('div', {}, el('p', {}, 'Your host has enabled live captions.'), other));
    assert.deepEqual(withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC)), []);
    assert.equal(other.clicks, 0);
});

test('the notices are checked often just after joining, then slowly', () => {
    assert.equal(zoomOverlayCheckDelayMs(0), 3_000);
    assert.equal(zoomOverlayCheckDelayMs(119_999), 3_000);
    assert.equal(zoomOverlayCheckDelayMs(120_000), 15_000);
    assert.equal(zoomOverlayCheckDelayMs(3_600_000), 15_000);
});
