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
 * Positional-click budget before falling back to a DOM click. CloakBrowser's
 * humanized actions retry Playwright-style until the action timeout and then
 * throw (they do not fall back to Playwright's own click), so without a bound a
 * miss would hold the join for the page default (20s) before the fallback runs.
 */
export const POSITIONAL_CLICK_TIMEOUT_MS = 5_000;

/** Synthetic DOM click on the nearest clickable ancestor; no viewport, overlay
 * or element-identity constraint. */
async function domClick(handle: ElementHandle<Element>): Promise<void> {
    await handle.evaluate((el: Element) => {
        const t = (el.closest('button, [role="button"], a') as HTMLElement | null) || (el as HTMLElement);
        t.scrollIntoView({ block: 'center', inline: 'center' });
        t.click();
    });
}

/**
 * Meaningful UX-style clicks (Join, Sign In, Skip-this-step). `force: true`
 * skips the "covered by another element" hit-test (the pre-join screen often
 * floats a transient overlay over the Join button). When the positional click
 * fails for any reason — the element is hidden, cannot be brought into the
 * viewport, or CloakBrowser cannot identify the element behind the handle
 * because an unrelated element shares its exact box — fall back to a DOM-level
 * click via evaluate, which scrolls the element into view and dispatches
 * click() directly.
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
            await target.click({ force: true, timeout: POSITIONAL_CLICK_TIMEOUT_MS });
        }
    } catch (err) {
        if (!handle) throw err;
        await domClick(handle);
    }
}

/**
 * Click an element with the normal (hit-tested, humanized) positional click,
 * falling back to a DOM-level click if it fails. For pre-join and in-meeting
 * controls whose miss would otherwise abort the join: a humanized click throws
 * instead of degrading (see POSITIONAL_CLICK_TIMEOUT_MS). Rethrows the original
 * error if the DOM click fails too (e.g. the element was detached).
 */
export async function clickWithDomFallback(
    handle: ElementHandle<Element>,
    label: string,
    timeout: number = POSITIONAL_CLICK_TIMEOUT_MS,
): Promise<void> {
    try {
        await handle.click({ timeout });
    } catch (err) {
        console.log(`[prejoin] ${label}: positional click failed (${(err as Error)?.message?.split('\n')[0]}); using DOM click`);
        try {
            await domClick(handle);
        } catch {
            throw err;
        }
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
