/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Google Meet handler (google-meet.ts) knows about Meet's web page,
 * kept apart from the handler so it can be tested without a browser: the
 * meeting-code and URL rules, the selectors, and the readings of page text that
 * decide where the VP is (pre-join, waiting to be let in, in the meeting, or on
 * one of Meet's refusal and end screens).
 *
 * Meet's CSS class names are generated and change between releases, so the
 * selectors lean on accessible names (`aria-label`), roles, `data-*` attributes
 * and visible text. Everything here was written from Meet's English web client
 * as publicly documented and observed, and has NOT been exercised against a live
 * meeting by this code yet. Each assumption that needs a live check is marked
 * "ASSUMPTION (verify live)" so they can be found and confirmed in one pass.
 *
 * The handler opens Meet with `hl=en`, so the text it matches is the English
 * text whatever the browser's locale.
 */

/** The only host the VP navigates to for a Meet meeting. */
export const MEET_HOST = 'meet.google.com';

/** A Meet meeting code: three, four and three lower-case letters. */
const CODE_PATTERN = /^([a-z]{3})-?([a-z]{4})-?([a-z]{3})$/;

/** A meeting the VP can open: its canonical code when it has one, and the URL to open. */
export interface GoogleMeetMeeting {
    /** `abc-defg-hij`, or null for a Meet URL that is not a plain meeting code (a lookup link). */
    code: string | null;
    /** The page the VP opens, always on https://meet.google.com and with `hl=en`. */
    url: string;
}

/**
 * Reduce what a user typed or pasted to a Meet meeting, or null when it is not one.
 *
 * Accepted: a bare code with or without hyphens and in any case (`abc-defg-hij`,
 * `ABCDEFGHIJ`), and a link on meet.google.com with or without the scheme
 * (`https://meet.google.com/abc-defg-hij?authuser=0`). Whitespace anywhere is
 * ignored, as the create form strips it. A link on any other host, including
 * one whose name merely starts with meet.google.com, is not a Meet meeting.
 * A meet.google.com link whose path is not a code (`/lookup/<name>`, which
 * Workspace calendar invitations have used) is accepted as a URL with no code.
 */
export function parseGoogleMeetMeeting(input: string | null | undefined): GoogleMeetMeeting | null {
    const raw = (input || '').replace(/\s+/g, '');
    if (!raw) return null;

    const bare = CODE_PATTERN.exec(raw.toLowerCase());
    if (bare) {
        const code = `${bare[1]}-${bare[2]}-${bare[3]}`;
        return { code, url: meetUrlFor(code) };
    }

    let parsed: URL;
    try {
        parsed = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : `https://${raw}`);
    } catch {
        return null;
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
    if (parsed.hostname.toLowerCase() !== MEET_HOST) return null;
    if (parsed.username || parsed.password || (parsed.port && parsed.port !== '443')) return null;

    const segments = parsed.pathname.split('/').filter(Boolean);
    if (segments.length === 0) return null;
    // Meet redirects https://meet.google.com/<code> to /meet/<code> (observed
    // live for a guest that was not signed in), so a link copied from the
    // address bar can carry either form.
    const codeSegment =
        segments.length === 1 ? segments[0] : segments.length === 2 && segments[0].toLowerCase() === 'meet' ? segments[1] : null;
    const first = codeSegment ? CODE_PATTERN.exec(codeSegment.toLowerCase()) : null;
    if (first) {
        const code = `${first[1]}-${first[2]}-${first[3]}`;
        return { code, url: meetUrlFor(code) };
    }
    // Any other meet.google.com path. The query is kept apart from `hl`, which
    // is forced to English below.
    const url = new URL(`https://${MEET_HOST}${parsed.pathname}`);
    parsed.searchParams.forEach((value, key) => {
        if (key.toLowerCase() !== 'hl') url.searchParams.append(key, value);
    });
    url.searchParams.set('hl', 'en');
    return { code: null, url: url.toString() };
}

/**
 * Is this URL where Meet sends a guest it has refused?
 *
 * Observed live: the "You can't join this video call" page counts down from
 * about a minute ("Returning to home screen") and then navigates to Meet's
 * product page on workspace.google.com. Reaching that page means the refusal
 * was missed or arrived while nothing was looking, and it is just as final.
 */
export function isMeetRefusalRedirect(url: string | null | undefined): boolean {
    try {
        const parsed = new URL(url || '');
        return parsed.hostname.toLowerCase() === 'workspace.google.com' && /^\/products\/meet(\/|$)/i.test(parsed.pathname);
    } catch {
        return false;
    }
}

function meetUrlFor(code: string): string {
    // ASSUMPTION (verify live): Meet honours `hl` for its own UI language, as
    // Google's other web properties do, so the text matched below is English.
    return `https://${MEET_HOST}/${code}?hl=en`;
}

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

/**
 * Selectors for the pre-join screen and the meeting, each a list tried in order.
 *
 * `:has-text("text")` is not CSS: it is Playwright's case-insensitive match on
 * an element's text, which `page.$` and findElementWithFallback understand and
 * `document.querySelector` does not. A list passed into the page with
 * `page.evaluate` must not use it; `MEET_IN_PAGE_SELECTORS` below holds those.
 */
export const MEET_SELECTORS = {
    /**
     * The guest name field under "What's your name?".
     * ASSUMPTION (verify live): aria-label and placeholder are both "Your name".
     */
    nameInput: [
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[type="text"][aria-label*="name" i]',
    ],
    /**
     * The join button. A guest normally sees "Ask to join"; "Join now" appears
     * when the meeting lets the VP in without asking.
     * ASSUMPTION (verify live): the buttons' visible text is exactly these, and
     * `jsname="Qx7uuf"` is the button's stable name in the current release.
     */
    joinButton: [
        'button:has-text("Ask to join")',
        'button:has-text("Join now")',
        '[role="button"]:has-text("Ask to join")',
        '[role="button"]:has-text("Join now")',
        'button[jsname="Qx7uuf"]',
    ],
    /**
     * The chat composer in the "In-call messages" panel.
     * ASSUMPTION (verify live): a textarea labelled "Send a message" (the LMA
     * Chrome extension's Meet integration posts through the same element).
     */
    chatInput: [
        'textarea[aria-label^="Send a message" i]',
        'textarea[placeholder^="Send a message" i]',
        '[contenteditable="true"][aria-label^="Send a message" i]',
    ],
    /** The composer's send button, used when Enter does not submit. */
    chatSend: ['button[aria-label^="Send a message" i]', 'button[aria-label="Send message" i]'],
    /**
     * The toolbar button that opens the chat panel. A toggle, so the handler
     * only clicks it when the composer is not already on screen.
     * ASSUMPTION (verify live): labelled "Chat with everyone".
     */
    chatButton: ['button[aria-label^="Chat with everyone" i]', 'button[aria-label*="in-call messages" i]'],
    /**
     * The toolbar button that opens the People panel.
     * ASSUMPTION (verify live): labelled "People" (or "Show everyone" in older
     * releases), with the number of participants shown inside it.
     */
    peopleButton: ['button[aria-label^="People" i]', 'button[aria-label^="Show everyone" i]'],
    /** A visible element in the People panel, for the consent screenshot. */
    peopleList: ['[role="list"][aria-label="Participants" i]', '[aria-label="Participants" i] [role="listitem"]'],
} as const;

/**
 * Selectors evaluated inside the page with `document.querySelector`, so plain
 * CSS only. Passed into `page.evaluate` as an argument rather than written inline,
 * which is what lets google-meet-page.test.ts check them.
 */
export const MEET_IN_PAGE_SELECTORS = {
    /**
     * Present only while the VP is in the meeting: the red hang-up button.
     * ASSUMPTION (verify live): labelled "Leave call".
     */
    leaveCall: ['button[aria-label="Leave call" i]', 'button[aria-label^="Leave call" i]', '[role="button"][aria-label^="Leave call" i]'],
    /** The pre-join join buttons, by the jsname of the current release only (text is checked separately). */
    prejoinJoin: ['button[jsname="Qx7uuf"]'],
    /** The People button, whose text carries the participant count. */
    peopleButton: ['button[aria-label^="People" i]', 'button[aria-label^="Show everyone" i]'],
    /** The chat composer, to tell whether the chat panel is open. */
    chatInput: [
        'textarea[aria-label^="Send a message" i]',
        'textarea[placeholder^="Send a message" i]',
        '[contenteditable="true"][aria-label^="Send a message" i]',
    ],
    /**
     * One chat message body. ASSUMPTION (verify live): each message carries a
     * `data-message-id`, which is also what the reader de-duplicates on.
     */
    chatMessage: '[data-message-id]',
    /**
     * The block that groups a run of messages from one sender under a header
     * with their name and the time. ASSUMPTION (verify live): marked with
     * `data-sender-id`. When it does not match, messages are read with no sender.
     */
    chatGroup: '[data-sender-id]',
    /**
     * A participant tile in the meeting grid. The LMA Chrome extension's Meet
     * integration reads the same attribute.
     */
    participantTile: '[data-participant-id]',
    /** Where a tile shows the participant's name. */
    tileName: ['[data-self-name]', '.notranslate'],
    /**
     * Elements inside a tile that show the speaking indicator while that
     * participant is heard. ASSUMPTION (verify live): Meet's speaking indicator
     * carries one of these generated class names in the current release. They
     * are the least stable thing in this file; the handler logs what it saw in
     * the first tiles it reads so they can be confirmed or replaced.
     */
    speakingIndicator: ['.Oaajhc', '.HX2H7', '.wEsLMd', '.OgVli'],
} as const;

// ---------------------------------------------------------------------------
// Screens, read from page text
// ---------------------------------------------------------------------------

/** A screen Meet shows that the handler has to act on. */
export type MeetScreen =
    /** Meet will not let this browser join ("You can't join this video call"). */
    | 'refused'
    /** Someone in the meeting declined the request to join. */
    | 'denied'
    /** The request to join timed out with nobody answering it. */
    | 'no-response'
    /** The code or link is not a meeting. */
    | 'invalid-code'
    /** The VP has asked to join and is waiting to be let in. */
    | 'waiting'
    /** Someone removed the VP from the meeting. */
    | 'removed'
    /** The meeting was ended for everyone. */
    | 'ended'
    /** Meet shows the page it shows after leaving a call. */
    | 'left';

/**
 * Phrases per screen, lower-case, with typographic apostrophes already folded to
 * `'`. Checked in the order of `SCREEN_ORDER`, most specific first, because some
 * screens share wording: a declined request is shown under the same "You can't
 * join this call" heading as a refusal.
 *
 * ASSUMPTION (verify live): every phrase below. They are Meet's English wording
 * as known when this was written; a live meeting may show different text, and a
 * screen whose wording is missed falls through to the AI join-state check during
 * the admission wait, and to the participant-count watchdog in the meeting.
 */
export const MEET_SCREEN_PHRASES: Readonly<Record<MeetScreen, readonly string[]>> = {
    denied: [
        'denied your request to join',
        'your request to join was denied',
        'your request to join was declined',
        "you've been denied entry",
    ],
    'no-response': [
        'no one responded to your request',
        'no one has responded to your request',
        'nobody responded to your request',
    ],
    removed: [
        "you've been removed from the meeting",
        'you have been removed from the meeting',
        "you've been removed from the call",
        'removed you from the meeting',
        'removed you from the call',
    ],
    ended: [
        'the call has ended',
        'this call has ended',
        'the meeting has ended',
        'this meeting has ended',
        'ended the call for everyone',
        'ended the meeting for everyone',
        'call ended for everyone',
    ],
    left: ['you left the meeting', 'you left the call', "you've left the call", "you've left the meeting"],
    // Observed live (guest, not signed in, browser on an EC2 host): the page
    // shows this as its <h1>, before any name field, with a "Return to home
    // screen" button and a countdown, and then navigates to
    // workspace.google.com/products/meet (see isMeetRefusalRedirect).
    refused: ["you can't join this video call", "you can't join this call"],
    'invalid-code': [
        'check your meeting code',
        'invalid video call name',
        "this video call doesn't exist",
        "the meeting code you entered doesn't exist",
    ],
    waiting: [
        'asking to be let in',
        "you'll join the call when someone lets you in",
        'please wait until a meeting host brings you into the call',
        'waiting for the host',
    ],
};

const SCREEN_ORDER: readonly MeetScreen[] = [
    'denied',
    'no-response',
    'removed',
    'ended',
    'left',
    'refused',
    'invalid-code',
    'waiting',
];

/** Lower-case, collapse whitespace, and fold the apostrophes Meet typesets. */
export function normalizeMeetText(text: string | null | undefined): string {
    return (text || '')
        .replace(/[‘’ʼ]/g, "'")
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();
}

/** Which screen this page text belongs to, or null when it is none of them. */
export function classifyMeetScreenText(text: string | null | undefined): { screen: MeetScreen; phrase: string } | null {
    const normalized = normalizeMeetText(text);
    if (!normalized) return null;
    for (const screen of SCREEN_ORDER) {
        const phrase = MEET_SCREEN_PHRASES[screen].find((p) => normalized.includes(p));
        if (phrase) return { screen, phrase };
    }
    return null;
}

// ---------------------------------------------------------------------------
// Participant count, names and chat senders
// ---------------------------------------------------------------------------

/**
 * The participant count shown on the People button, or null when none can be read.
 *
 * The button's text is the icon's ligature name followed by the count (Meet's
 * icons are font ligatures, so the text reads like "people 3"); the label is
 * checked second in case a release moves the count there. A value of 0 is
 * treated as unreadable, since the VP itself is always in the meeting.
 * ASSUMPTION (verify live): the count includes the VP.
 */
export function parseMeetParticipantCount(text: string | null | undefined, label?: string | null): number | null {
    for (const source of [text, label]) {
        const numbers = (source || '').match(/\d{1,4}/g);
        if (!numbers || numbers.length !== 1) continue;
        const n = Number.parseInt(numbers[0], 10);
        if (Number.isFinite(n) && n > 0) return n;
    }
    return null;
}

/**
 * A participant name as a tile or chat header shows it, without Meet's decorations.
 *
 * Meet adds "(You)" to the VP's own tile and "(Presentation)" / "is presenting"
 * to a screen share's tile. A label that is only a decoration gives null.
 */
export function cleanMeetParticipantName(raw: string | null | undefined): string | null {
    let name = (raw || '').replace(/\s+/g, ' ').trim();
    if (!name) return null;
    name = name
        .replace(/\s*\((?:you|presentation|presenting|host|co-host)\)\s*$/i, '')
        .replace(/\s+is presenting$/i, '')
        .replace(/\s*\(presentation\)/i, '')
        .trim();
    if (!name || name.length > 100) return null;
    // A Material icon ligature ("visual_effects", "mic_off") is the text of an
    // icon inside the tile, not a person's name.
    if (/^[a-z0-9]+(?:_[a-z0-9]+)+$/.test(name)) return null;
    return name;
}

/**
 * The sender named by a chat group's header, or null.
 *
 * The header reads "<name> <time>" ("Alice Smith 10:32 AM"); the VP's own read
 * "You". The text passed in is the group's text with its message bodies removed.
 * It must hold exactly one time: a text with two is more than one group, and
 * taking the first name from it would credit one person's message to another.
 */
export function senderFromMeetGroupHeader(header: string | null | undefined): string | null {
    const text = (header || '').replace(/\s+/g, ' ').trim();
    if (!text) return null;
    const times = text.match(/\b\d{1,2}:\d{2}(?:\s*[ap]\.?m\.?)?/gi) ?? [];
    if (times.length > 1) return null;
    const name = (times.length === 1 ? text.slice(0, text.search(/\b\d{1,2}:\d{2}/)) : text).trim();
    if (!name || name.length > 80) return null;
    return cleanMeetParticipantName(name);
}

/**
 * Is this name the VP's own?
 *
 * Meet labels the VP's own chat messages "You" and its own tile with its display
 * name. Matched whole, case-insensitively, so a participant whose name merely
 * contains "LMA" or starts with "You" is not mistaken for the VP.
 */
export function isOwnMeetName(name: string | null | undefined, identities: readonly string[]): boolean {
    const candidate = cleanMeetParticipantName(name);
    if (!candidate) return false;
    const folded = candidate.toLowerCase();
    if (folded === 'you') return true;
    return identities.some((own) => own && own.trim().toLowerCase() === folded);
}

/** One participant tile, as the speaker poll reads it from the page. */
export interface MeetTileReading {
    name: string | null;
    speaking: boolean;
}

/**
 * The participant to report as speaking, or null to leave the current one.
 *
 * Tiles that are not speaking, have no name, or are the VP's own are ignored.
 * When several are speaking, the current speaker is kept if still among them,
 * since talking over each other is brief; otherwise the first one wins. When
 * every one of three or more named tiles reads as speaking, the indicator is not
 * telling them apart (the class list no longer matches Meet's), and nobody is
 * reported rather than the wrong person.
 */
export function pickMeetActiveSpeaker(
    tiles: readonly MeetTileReading[],
    identities: readonly string[],
    current: string | null,
): string | null {
    const named = tiles
        .map((tile) => ({ name: cleanMeetParticipantName(tile.name), speaking: tile.speaking }))
        .filter((tile): tile is { name: string; speaking: boolean } => !!tile.name && !isOwnMeetName(tile.name, identities));
    const distinct = new Map<string, boolean>();
    for (const tile of named) distinct.set(tile.name, (distinct.get(tile.name) ?? false) || tile.speaking);
    const speaking = [...distinct].filter(([, isSpeaking]) => isSpeaking).map(([name]) => name);
    if (speaking.length === 0) return null;
    if (distinct.size >= 3 && speaking.length === distinct.size) return null;
    if (current && speaking.includes(current)) return current;
    return speaking[0];
}
