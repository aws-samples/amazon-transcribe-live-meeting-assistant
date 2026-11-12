/**
 * Dismissing the Zoom web client's own notices once the VP is in the meeting.
 *
 * Zoom floats feature announcements and advisories over the meeting view
 * after joining: a "Introducing My Notes" callout with a "Got it" button, and
 * a banner advising to enable graphics/hardware acceleration with a close (x)
 * button. They are not modal dialogs (they sit outside `.zm-modal` and carry no
 * consent or recording wording), so the post-join popup handler in zoom.ts
 * leaves them, and they stay on the VP's screen for the whole meeting -- over
 * the shared screen in the recording and in the key frames.
 *
 * dismissZoomOverlaysInPage finds them by what they say rather than by Zoom's
 * class names: a visible control that dismisses ("Got it", "Close", "x", a
 * close icon, ...) inside a small element whose text matches one of the
 * notices. "Small" (short text, few controls) bounds the search so the
 * control's notice is found, not the meeting view around it. It runs
 * periodically for the whole meeting (zoom.ts), so a notice that comes back is
 * dismissed again, and it reports a visible notice it could not dismiss, with
 * the controls around it, so the log shows why.
 *
 * Nothing here imports Playwright: the in-page function is passed whole to
 * page.evaluate, so it is unit-tested against a fake DOM.
 */

export interface ZoomOverlaySpec {
    /** Lower-case text fragments; a notice's text contains one of them. */
    noticeText: readonly string[];
    /**
     * Lower-case accessible names / texts of a button that dismisses a notice.
     * A name starting with "close " or "dismiss " ("Close banner") counts too,
     * and so does an unnamed control marked as a close icon by its class, or
     * the only unnamed control of the notice.
     */
    dismissNames: readonly string[];
    /** Lower-case fragments of a button name never clicked (the meeting's own panels). */
    neverClick: readonly string[];
    /** Elements that can be the dismiss button. */
    buttons: string;
    /** A notice's text is at most this long; a longer element is the page around it. */
    maxNoticeChars: number;
    /**
     * A notice holds at most this many controls (outermost only, links not
     * counted); an element with more is the meeting view (toolbar, panels)
     * around it.
     */
    maxButtons: number;
    /** How many ancestors above the button are searched for the notice. */
    maxDepth: number;
}

export const ZOOM_OVERLAY_SPEC: ZoomOverlaySpec = {
    noticeText: ['introducing my notes', 'hardware acceleration'],
    dismissNames: ['got it', 'close', 'dismiss', 'ok', 'okay', 'not now', 'maybe later', 'x', '×', '✕', '✖'],
    neverClick: ['chat', 'participant', 'leave', 'end meeting', 'mute', 'video', 'share', 'record'],
    buttons: 'button, [role="button"], [class*="close" i]',
    maxNoticeChars: 400,
    maxButtons: 3,
    maxDepth: 8,
};

/** One dismissal, reported back to Node for the log. */
export interface ZoomOverlayDismissal {
    /** The notice's text, shortened. */
    notice: string;
    /** The name of the button clicked. */
    button: string;
}

/**
 * A visible notice that was not dismissed, with the controls found around it,
 * so a notice whose markup these rules do not cover can be fixed from the log.
 */
export interface ZoomOverlayMiss {
    /** The notice's text, shortened. */
    notice: string;
    /** The controls nearest to it: tag, role, class and name of each, shortened. */
    controls: string;
}

export interface ZoomOverlayResult {
    dismissed: ZoomOverlayDismissal[];
    missed: ZoomOverlayMiss[];
}

/**
 * Runs in the page. Clicks the dismiss control of every visible Zoom notice the
 * spec describes, and returns what it dismissed and which visible notices it
 * could not dismiss. Self-contained: page.evaluate serialises it without the
 * rest of this module.
 *
 * A dismiss control is named as a dismissal ("Got it", "Close", "x", "Close
 * banner", ...), or is unnamed and marked as a close icon by its class, or is
 * unnamed and the only unnamed control of its notice (an icon-only x). A
 * control whose name contains a never-click fragment is never clicked. Its
 * notice is the nearest ancestor whose text contains a notice's wording and
 * that is small: short text and few controls. Controls are counted outermost
 * only (an icon inside a close button is one control), and links ("Learn
 * more") are not counted.
 */
export function dismissZoomOverlaysInPage(spec: ZoomOverlaySpec): ZoomOverlayResult {
    const visible = (el: Element): boolean => {
        const r = (el as HTMLElement).getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const norm = (s: string | null | undefined): string => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const nameOf = (el: Element): string =>
        norm(el.getAttribute('aria-label')) || norm(el.getAttribute('title')) || norm(el.textContent);
    const isLink = (el: Element): boolean =>
        (el.tagName || '').toUpperCase() === 'A' || el.getAttribute('role') === 'link';
    /** The controls inside `node`, outermost only, links excluded. */
    const controlsIn = (node: Element): Element[] =>
        Array.from(node.querySelectorAll(spec.buttons)).filter((c) => {
            if (isLink(c)) return false;
            for (let p = c.parentElement; p && p !== node; p = p.parentElement) {
                if (p.matches(spec.buttons)) return false;
            }
            return true;
        });
    const closeMarked = (el: Element): boolean =>
        /close/i.test(el.getAttribute('class') || '') || el.querySelector('[class*="close" i]') !== null;
    // An SVG icon has no click(); dispatch the event a click would.
    const press = (el: Element): void => {
        const h = el as HTMLElement;
        if (typeof h.click === 'function') {
            h.click();
            return;
        }
        const ev =
            typeof MouseEvent === 'function'
                ? new MouseEvent('click', { bubbles: true, cancelable: true })
                : new Event('click', { bubbles: true, cancelable: true });
        el.dispatchEvent(ev);
    };

    const dismissed: ZoomOverlayDismissal[] = [];
    /** The notice wordings dismissed in this pass (the reported text is shortened). */
    const dismissedTexts = new Set<string>();
    const clicked = new Set<Element>();
    for (const button of Array.from(document.querySelectorAll(spec.buttons))) {
        try {
            if (!visible(button) || isLink(button)) continue;
            const name = nameOf(button);
            const unnamed = name === '';
            const named = spec.dismissNames.includes(name) || /^(close|dismiss) /.test(name);
            if (!named && !unnamed) continue;
            if (spec.neverClick.some((n) => name.includes(n))) continue;

            let node: Element | null = button.parentElement;
            for (let depth = 0; node && depth < spec.maxDepth; depth++, node = node.parentElement) {
                const text = norm(node.textContent);
                if (text.length > spec.maxNoticeChars) break;
                const controls = controlsIn(node);
                if (controls.length > spec.maxButtons) break;
                if (!spec.noticeText.some((t) => text.includes(t))) continue;
                if (clicked.has(node)) break;
                // An unmarked icon counts only as the notice's single unnamed
                // control; with two there is no telling which one closes it.
                if (unnamed && !closeMarked(button) && controls.filter((c) => nameOf(c) === '').length !== 1) break;
                clicked.add(node);
                press(button);
                dismissed.push({ notice: text.slice(0, 80), button: name || 'close icon' });
                for (const t of spec.noticeText) if (text.includes(t)) dismissedTexts.add(t);
                break;
            }
        } catch {
            // One odd element must not stop the other notices being dismissed.
        }
    }

    // Visible notices still on screen, described for the log.
    const missed: ZoomOverlayMiss[] = [];
    try {
        const root = ((document as unknown as { body?: Element }).body || document) as unknown as Element;
        for (const t of spec.noticeText) {
            if (dismissedTexts.has(t)) continue;
            if (!norm(root.textContent).includes(t)) continue;
            // The deepest element holding the wording is the notice's text.
            let leaf: Element = root;
            for (;;) {
                const next = Array.from(leaf.children || []).find((c) => norm(c.textContent).includes(t));
                if (!next) break;
                leaf = next;
            }
            if (leaf === root || !visible(leaf)) continue;
            const leafText = norm(leaf.textContent);
            if (leafText.length > spec.maxNoticeChars) continue;
            let controls: Element[] = [];
            for (let node: Element | null = leaf, depth = 0; node && depth <= spec.maxDepth; depth++) {
                controls = Array.from(node.querySelectorAll(spec.buttons));
                if (controls.length) break;
                node = node.parentElement;
            }
            const describe = (c: Element): string => {
                const role = c.getAttribute('role');
                const cls = (c.getAttribute('class') || '').slice(0, 60);
                const tag = (c.tagName || '').toLowerCase();
                return `${tag}${role ? `[role=${role}]` : ''}${cls ? `.${cls}` : ''} name="${nameOf(c).slice(0, 30)}"`;
            };
            missed.push({
                notice: leafText.slice(0, 80),
                controls: controls.length ? controls.slice(0, 5).map(describe).join('; ') : 'none found',
            });
        }
    } catch {
        /* diagnostics only */
    }
    return { dismissed, missed };
}

/**
 * When the dismisser looks again: often just after joining, when Zoom shows
 * its notices, then at a slow pace for the rest of the meeting in case one
 * comes back.
 */
export function zoomOverlayCheckDelayMs(sinceJoinMs: number): number {
    return sinceJoinMs < 120_000 ? 3_000 : 15_000;
}
