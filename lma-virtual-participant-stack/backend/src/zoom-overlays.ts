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
 * class names: a visible button whose name is a dismissal ("Got it", "Close",
 * "x", ...) inside a small element whose text matches one of the notices.
 * "Small" (short text, few buttons) bounds the search so the button's notice
 * is found, not the meeting view around it. It runs periodically for the whole meeting (zoom.ts), so a
 * notice that comes back is dismissed again.
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
     * and so does an unnamed button marked as a close icon by its class.
     */
    dismissNames: readonly string[];
    /** Lower-case fragments of a button name never clicked (the meeting's own panels). */
    neverClick: readonly string[];
    /** Elements that can be the dismiss button. */
    buttons: string;
    /** A notice's text is at most this long; a longer element is the page around it. */
    maxNoticeChars: number;
    /**
     * A notice holds at most this many buttons; an element with more is the
     * meeting view (toolbar, panels) around it.
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
    maxDepth: 6,
};

/** One dismissal, reported back to Node for the log. */
export interface ZoomOverlayDismissal {
    /** The notice's text, shortened. */
    notice: string;
    /** The name of the button clicked. */
    button: string;
}

/**
 * Runs in the page. Clicks the dismiss button of every visible Zoom notice the
 * spec describes, and returns what it dismissed (empty when there was none).
 * Self-contained: page.evaluate serialises it without the rest of this module.
 */
export function dismissZoomOverlaysInPage(spec: ZoomOverlaySpec): ZoomOverlayDismissal[] {
    const visible = (el: Element): boolean => {
        const r = (el as HTMLElement).getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const norm = (s: string | null | undefined): string => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const nameOf = (el: Element): string =>
        norm(el.getAttribute('aria-label')) || norm(el.getAttribute('title')) || norm(el.textContent);

    const done: ZoomOverlayDismissal[] = [];
    const clicked = new Set<Element>();
    for (const button of Array.from(document.querySelectorAll(spec.buttons))) {
        if (!visible(button)) continue;
        const name = nameOf(button);
        const closeIcon =
            name === '' &&
            (/close/i.test(button.getAttribute('class') || '') || button.querySelector('[class*="close" i]') !== null);
        const dismisses = spec.dismissNames.includes(name) || /^(close|dismiss) /.test(name) || closeIcon;
        if (!dismisses) continue;
        if (spec.neverClick.some((n) => name.includes(n))) continue;

        let node: Element | null = button.parentElement;
        for (let depth = 0; node && depth < spec.maxDepth; depth++, node = node.parentElement) {
            const text = norm(node.textContent);
            if (text.length > spec.maxNoticeChars) break;
            if (node.querySelectorAll(spec.buttons).length > spec.maxButtons) break;
            if (!spec.noticeText.some((t) => text.includes(t))) continue;
            if (clicked.has(node)) break;
            clicked.add(node);
            (button as HTMLElement).click();
            done.push({ notice: text.slice(0, 80), button: name || 'close icon' });
            break;
        }
    }
    return done;
}

/**
 * When the dismisser looks again: often just after joining, when Zoom shows
 * its notices, then at a slow pace for the rest of the meeting in case one
 * comes back.
 */
export function zoomOverlayCheckDelayMs(sinceJoinMs: number): number {
    return sinceJoinMs < 120_000 ? 3_000 : 15_000;
}
