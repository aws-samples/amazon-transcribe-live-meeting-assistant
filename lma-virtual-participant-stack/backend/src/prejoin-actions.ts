/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Overlay-tolerant click / type helpers shared by every platform pre-join
 * flow (Zoom, Teams, …).
 *
 * Web meeting clients float transient overlays (loading shrouds, tooltips,
 * device-permission shrouds, marketing nudges) over the pre-join form for a
 * few hundred ms after it renders. Playwright's positional actions run an
 * actionability "pointer_events" hit-test first and throw
 *   Element "<ElementHandle>" failed pointer_events check: element is covered by <unknown>
 * when something is on top of the target — even though the element is the one
 * a human would obviously interact with. These helpers fall back to a DOM-level
 * focus/click that ignores the overlay so the join survives the transient.
 */
import { Page, ElementHandle } from 'playwright-core';

/**
 * Meaningful UX-style clicks (Join, Sign In, Skip-this-step). `force: true`
 * skips Playwright's "covered by another element" actionability check (the
 * pre-join screen often floats a transient overlay over the Join button).
 * `force: true` does NOT bypass the viewport check, though — when a client
 * opens a transient window it can push a button out of the viewport, and a
 * positional click then throws "Element is outside of the viewport". So on any
 * click failure we fall back to a DOM-level click via evaluate, which scrolls
 * the element into view and dispatches click() directly — no viewport or
 * actionability constraint.
 */
export async function humanClick(
    page: Page,
    target: string | ElementHandle<Element>,
): Promise<void> {
    const handle: ElementHandle<Element> | null =
        typeof target === 'string' ? await page.$(target) : target;
    try {
        if (typeof target === 'string') {
            await page.click(target, { force: true });
        } else {
            await target.click({ force: true });
        }
    } catch (err) {
        // Positional click failed (commonly off-viewport or covered). Fall
        // back to a synthetic DOM click that ignores viewport/overlay checks.
        if (!handle) throw err;
        await handle.evaluate((el: Element) => {
            const t = (el.closest('button, [role="button"], a') as HTMLElement | null) || (el as HTMLElement);
            t.scrollIntoView({ block: 'center', inline: 'center' });
            t.click();
        });
    }
}

/**
 * Type into a (possibly overlay-covered) input. Playwright's ElementHandle
 * .type() runs an actionability/pointer check that fails when the client
 * floats a transient overlay over the prejoin form. Focus the element directly
 * in the DOM (no actionability check), then type via the keyboard with a small
 * per-keystroke delay so input lands reliably in the client's SPA fields.
 */
export async function humanType(
    page: Page,
    element: ElementHandle<Element>,
    text: string,
): Promise<void> {
    await element.evaluate((el) => (el as HTMLElement).focus());
    await page.keyboard.type(text, { delay: 50 + Math.floor(Math.random() * 70) });
}
