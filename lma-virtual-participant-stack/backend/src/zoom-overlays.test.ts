/**
 * Zoom's in-meeting notices are dismissed; nothing else in the meeting is.
 *
 * dismissZoomOverlaysInPage runs in the page, so it is run here against a
 * small fake DOM shaped like what the Zoom web client (November 2026) showed
 * on the VP's screen: the "Introducing My Notes" callout with its "Got it"
 * button, and the banner advising to enable graphics/hardware acceleration
 * with a close (x) control. Neither is inside a `.zm-modal`. The banner's exact
 * markup was not captured from the live meeting; the banner tests cover the
 * shapes such a close control commonly takes (#1097).
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
    dispatched: string[] = [];
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
    get tagName(): string {
        return this.tag.toUpperCase();
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
    dispatchEvent(ev: { type: string }): boolean {
        this.dispatched.push(ev.type);
        return true;
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

/** An SVG element: no click() method, like a real SVGElement. */
class FakeSvgElement extends FakeElement {
    constructor(attrs: Record<string, string> = {}, children: Array<FakeElement | string> = []) {
        super('svg', attrs, children);
        (this as any).click = undefined;
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

const dismiss = (body: FakeElement) => withPage(body, () => dismissZoomOverlaysInPage(ZOOM_OVERLAY_SPEC));

const BANNER_TEXT =
    "For a better meeting experience, enable the option 'Use graphics/hardware acceleration' under browser system settings.";

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
        el('span', {}, BANNER_TEXT, el('a', {}, 'Learn more')),
        close,
    );
    return { banner, close };
}

/** The meeting around the notices: a toolbar and the chat panel. */
function meeting(...notices: FakeElement[]) {
    const chatClose = el('button', { 'aria-label': 'close the chat panel' });
    const leave = el('button', {}, 'Leave');
    // Icon-only toolbar controls, unnamed like a close icon.
    const iconA = el('button', { class: 'footer-button__icon' });
    const iconB = el('div', { role: 'button', class: 'footer-more' });
    const chat = el(
        'div',
        { class: 'chat-container' },
        el('div', {}, 'Meeting Chat'),
        chatClose,
        el('div', { role: 'row', 'aria-label': 'Bob to Everyone, 3:02 PM, introducing my notes is new, close it' }, 'Introducing My Notes is new'),
    );
    const toolbar = el('div', { class: 'footer' }, el('button', {}, 'Mute'), el('button', {}, 'Chat'), iconA, iconB, leave);
    const body = el('body', {}, el('div', { id: 'root' }, toolbar, chat, ...notices));
    return { body, chatClose, leave, iconA, iconB };
}

test('the My Notes callout is dismissed with its "Got it" button', () => {
    const { callout, gotIt } = myNotesCallout();
    const { body } = meeting(callout);
    const { dismissed, missed } = dismiss(body);
    assert.equal(gotIt.clicks, 1);
    assert.equal(dismissed.length, 1);
    assert.equal(dismissed[0].button, 'got it');
    assert.match(dismissed[0].notice, /^introducing my notes/);
    assert.deepEqual(missed, []);
});

test('the hardware-acceleration banner is closed with its unlabelled close icon', () => {
    const { banner, close } = accelerationBanner();
    const { body } = meeting(banner);
    const { dismissed } = dismiss(body);
    assert.equal(close.clicks, 1);
    assert.deepEqual(dismissed.map((d) => d.button), ['close icon']);
});

test('the banner is closed when its x is a nested close button and "Learn more" is a button', () => {
    // Zoom-style markup: the text and a "Learn more" link-button in one
    // wrapper, and a close button whose icon sits in a wrapper that is also
    // classed "close". Counting every matching element made this four
    // controls, over the limit of three; outermost controls are two.
    const icon = el('i', { class: 'zm-icon zm-icon-close' });
    const iconWrap = el('span', { class: 'notification-bar__close-icon-wrap' }, icon);
    const close = el('button', { type: 'button', class: 'zmu-btn notification-bar__close-btn' }, iconWrap);
    const learnMore = el('button', { type: 'button', class: 'zmu-btn zmu-btn--link' }, 'Learn more');
    const banner = el(
        'div',
        { class: 'notification-bar notification-bar--top', role: 'alert' },
        el('div', { class: 'notification-bar__content' }, el('span', { class: 'notification-bar__text' }, BANNER_TEXT), learnMore),
        close,
    );
    const { body } = meeting(banner);
    const { dismissed, missed } = dismiss(body);
    assert.equal(close.clicks, 1);
    assert.equal(learnMore.clicks, 0);
    assert.equal(iconWrap.clicks + icon.clicks, 0);
    assert.equal(dismissed.length, 1);
    assert.match(dismissed[0].notice, /hardware accele/);
    assert.deepEqual(missed.filter((m) => m.notice.includes('hardware')), [], 'a dismissed banner is not reported');
});

test('the banner is closed when its x is an icon with no name and no close class', () => {
    // An icon-only x: role=button, an SVG inside, no label, no "close" class.
    // It is the notice's only unnamed control, so it is the close control.
    const x = el('div', { role: 'button', tabindex: '0', class: 'banner__dismiss-x' }, new FakeSvgElement({ class: 'svg-icon' }));
    const banner = el(
        'div',
        { class: 'top-banner' },
        el('span', {}, BANNER_TEXT, el('a', { href: 'https://support.zoom.com/' }, 'Learn more')),
        x,
    );
    const { body } = meeting(banner);
    const { dismissed } = dismiss(body);
    assert.equal(x.clicks, 1);
    assert.deepEqual(dismissed.map((d) => d.button), ['close icon']);
});

test('a close icon that is an SVG element is clicked by dispatching the event', () => {
    // SVG elements have no click(); calling it used to abort the whole check.
    const svg = new FakeSvgElement({ class: 'icon-close' });
    const banner = el('div', { class: 'top-banner' }, el('span', {}, BANNER_TEXT), svg);
    const { callout, gotIt } = myNotesCallout();
    const { body } = meeting(banner, callout);
    const { dismissed } = dismiss(body);
    assert.deepEqual(svg.dispatched, ['click']);
    assert.equal(gotIt.clicks, 1, 'the other notice is still dismissed');
    assert.equal(dismissed.length, 2);
});

test('a banner with two unnamed, unmarked controls is left alone and reported', () => {
    const a = el('div', { role: 'button', class: 'banner__a' });
    const b = el('div', { role: 'button', class: 'banner__b' });
    const banner = el('div', { class: 'top-banner' }, el('span', {}, BANNER_TEXT), a, b);
    const { body } = meeting(banner);
    const { dismissed, missed } = dismiss(body);
    assert.equal(a.clicks + b.clicks, 0);
    assert.deepEqual(dismissed, []);
    const banners = missed.filter((m) => m.notice.includes('hardware'));
    assert.equal(banners.length, 1);
    assert.match(banners[0].controls, /div\[role=button\]\.banner__a name=""/);
});

test('a banner control named for the meeting is never clicked', () => {
    const stopVideo = el('button', { 'aria-label': 'Close and stop video' });
    const banner = el('div', { class: 'top-banner' }, el('span', {}, BANNER_TEXT), stopVideo);
    const { body } = meeting(banner);
    const { dismissed } = dismiss(body);
    assert.equal(stopVideo.clicks, 0);
    assert.deepEqual(dismissed, []);
});

test('both notices on screen together are both dismissed, each once', () => {
    const { callout, gotIt } = myNotesCallout();
    const { banner, close } = accelerationBanner();
    // A labelled button holding the icon: one click for the banner, not two.
    const labelled = el('button', { 'aria-label': 'Close' }, close);
    banner.children.splice(banner.children.indexOf(close), 1, labelled);
    labelled.parentElement = banner;
    close.parentElement = labelled;
    const { body } = meeting(callout, banner);
    const { dismissed } = dismiss(body);
    assert.equal(gotIt.clicks, 1);
    assert.equal(labelled.clicks + close.clicks, 1);
    assert.equal(dismissed.length, 2);
});

test('the meeting itself is left alone, even when the chat mentions a notice', () => {
    const { body, chatClose, leave, iconA, iconB } = meeting();
    const { dismissed } = dismiss(body);
    assert.deepEqual(dismissed, []);
    assert.equal(chatClose.clicks, 0);
    assert.equal(leave.clicks, 0);
    assert.equal(iconA.clicks + iconB.clicks, 0);
});

test('a hidden notice is not clicked', () => {
    const { callout, gotIt } = myNotesCallout();
    callout.hidden = true;
    const { body } = meeting(callout);
    assert.deepEqual(dismiss(body).dismissed, []);
    assert.equal(gotIt.clicks, 0);
});

test('a "Got it" outside any known notice is not clicked', () => {
    const other = el('button', {}, 'Got it');
    const { body } = meeting(el('div', {}, el('p', {}, 'Your host has enabled live captions.'), other));
    assert.deepEqual(dismiss(body).dismissed, []);
    assert.equal(other.clicks, 0);
});

test('the notices are checked often just after joining, then slowly', () => {
    assert.equal(zoomOverlayCheckDelayMs(0), 3_000);
    assert.equal(zoomOverlayCheckDelayMs(119_999), 3_000);
    assert.equal(zoomOverlayCheckDelayMs(120_000), 15_000);
    assert.equal(zoomOverlayCheckDelayMs(3_600_000), 15_000);
});
