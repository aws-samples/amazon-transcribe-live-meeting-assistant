/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { zoomPasswordKind } from './meeting-password.js';
import { createLogger, registerSecretValue } from './logger.js';

const log = createLogger('zoom-join-link');

/** The meeting number and `pwd` value a Zoom join link carries. */
export interface ZoomJoinLink {
    meetingId: string;
    pwd?: string;
}

// https://zoom.us/... or https://<subdomain>.zoom.us/..., e.g. us05web.zoom.us.
const ZOOM_HOST = /^([a-z0-9-]+\.)*zoom\.us$/i;
// /j/<number> (the invitation link), and /wc/<number>/join and
// /wc/join/<number> (the web client).
const ZOOM_PATHS = [/^\/j\/(\d+)\/?$/, /^\/wc\/(\d+)(?:\/join)?\/?$/, /^\/wc\/join\/(\d+)\/?$/];

/**
 * The meeting number and `pwd` value of a Zoom join link, or null when the
 * value is not a Zoom link in one of the forms above. A personal meeting link
 * (`/my/<name>`) does not carry the meeting number the web client is opened
 * with: see {@link parseZoomPersonalLink}.
 */
export function parseZoomJoinLink(value: string | undefined | null): ZoomJoinLink | null {
    const text = (value ?? '').trim();
    if (!/^https?:\/\//i.test(text)) return null;
    let url: URL;
    try {
        url = new URL(text);
    } catch {
        return null;
    }
    if (!ZOOM_HOST.test(url.hostname)) return null;
    for (const pattern of ZOOM_PATHS) {
        const match = pattern.exec(url.pathname);
        if (match) {
            const pwd = (url.searchParams.get('pwd') ?? '').trim();
            return pwd ? { meetingId: match[1], pwd } : { meetingId: match[1] };
        }
    }
    return null;
}

/**
 * The Zoom meeting ID and password the Virtual Participant joins with, when
 * the meeting ID it was given may be a Zoom join link. A link is reduced to
 * its meeting number. Its `pwd` value becomes the password unless a passcode
 * was given separately, since a passcode is what the join screen asks for; a
 * separately given password that is itself a link token gives way to the
 * link's own, which is complete. A meeting ID that is not a Zoom link is
 * returned as it is.
 */
export function resolveZoomMeeting(
    meetingId: string,
    meetingPassword: string,
): { meetingId: string; meetingPassword: string } {
    const link = parseZoomJoinLink(meetingId);
    if (!link) return { meetingId, meetingPassword };
    return { meetingId: link.meetingId, meetingPassword: joinPassword(meetingPassword, link.pwd) };
}

/**
 * The password to join with, given the one entered separately and the `pwd`
 * value of the link: the entered one when it is a passcode, else the link's,
 * else the entered one.
 */
function joinPassword(given: string, linkPwd: string | undefined): string {
    const givenIsPasscode = !!given && zoomPasswordKind(given) === 'passcode';
    return givenIsPasscode ? given : linkPwd || given;
}

/** A Zoom personal meeting link: `https://<host>.zoom.us/my/<name>`, with or without `pwd`. */
export interface ZoomPersonalLink {
    /** The link as given, trimmed. */
    url: string;
    host: string;
    name: string;
    pwd?: string;
}

const ZOOM_PERSONAL_PATH = /^\/my\/([^/]+)\/?$/;

/**
 * The parts of a Zoom personal meeting link, or null when the value is not
 * one. Zoom answers a request for such a link with a redirect to the
 * `/j/<number>` link of the room, which {@link resolveZoomPersonalLink}
 * follows once before the join.
 */
export function parseZoomPersonalLink(value: string | undefined | null): ZoomPersonalLink | null {
    const text = (value ?? '').trim();
    if (!/^https?:\/\//i.test(text)) return null;
    let url: URL;
    try {
        url = new URL(text);
    } catch {
        return null;
    }
    if (!ZOOM_HOST.test(url.hostname)) return null;
    const match = ZOOM_PERSONAL_PATH.exec(url.pathname);
    if (!match) return null;
    const pwd = (url.searchParams.get('pwd') ?? '').trim();
    // The link is requested over https whatever scheme it was given with: its
    // query string can carry the meeting's pwd value.
    url.protocol = 'https:';
    const link: ZoomPersonalLink = { url: url.toString(), host: url.hostname, name: match[1] };
    return pwd ? { ...link, pwd } : link;
}

/**
 * The meeting number and `pwd` value of the `Location` Zoom answered a
 * personal meeting link with, or null when it is not a `/j/<number>` link on
 * a Zoom host. A relative `Location` is read against the personal link.
 */
export function meetingFromZoomRedirect(
    location: string | undefined | null,
    personalLinkUrl: string,
): ZoomJoinLink | null {
    const text = (location ?? '').trim();
    if (!text) return null;
    let url: URL;
    try {
        url = new URL(text, personalLinkUrl);
    } catch {
        return null;
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    if (!ZOOM_HOST.test(url.hostname)) return null;
    const match = /^\/j\/(\d+)\/?$/.exec(url.pathname);
    if (!match) return null;
    const pwd = (url.searchParams.get('pwd') ?? '').trim();
    return pwd ? { meetingId: match[1], pwd } : { meetingId: match[1] };
}

/** One request for a URL, not following a redirect: the status and the `Location` header. */
export type ZoomRedirectRequest = (url: string) => Promise<{ status: number; location: string | null }>;

/** How long the request for a personal meeting link may take. */
export const ZOOM_PERSONAL_LINK_TIMEOUT_MS = 10_000;

// An ordinary desktop browser's User-Agent: the redirect is the one a browser
// opening the link gets.
const BROWSER_USER_AGENT =
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

/** {@link ZoomRedirectRequest} over the network: one GET, redirects not followed. */
export const requestZoomRedirect: ZoomRedirectRequest = async (url) => {
    const response = await fetch(url, {
        method: 'GET',
        redirect: 'manual',
        headers: { 'User-Agent': BROWSER_USER_AGENT, Accept: 'text/html' },
        signal: AbortSignal.timeout(ZOOM_PERSONAL_LINK_TIMEOUT_MS),
    });
    // The body is not needed; release the connection.
    await response.body?.cancel().catch(() => undefined);
    return { status: response.status, location: response.headers.get('location') };
};

/** A personal meeting link resolved to the meeting the Virtual Participant joins. */
export interface ResolvedZoomPersonalLink {
    meetingId: string;
    meetingPassword: string;
    /** The `pwd` value of the redirect, which is a secret like the link's own. */
    redirectPwd?: string;
}

/** Why a personal meeting link was not resolved: the request failed, or Zoom did not redirect to a meeting. */
export type ZoomPersonalLinkFailure = { failure: 'request-failed'; detail: string } | { failure: 'no-meeting'; status: number };

/**
 * The meeting a Zoom personal meeting link leads to, found with one request
 * for the link that does not follow Zoom's redirect: the redirect's
 * `/j/<number>` gives the meeting number. The password is the separately
 * given passcode, else the link's `pwd` value, else the redirect's, which
 * then goes on the web-client URL exactly as a `/j/<number>?pwd=` link's does.
 *
 * Zoom redirects a personal link whose name it does not know to a meeting
 * number too, so a resolved link can still lead to a meeting that does not
 * exist; the web client says so after the join.
 */
export async function resolveZoomPersonalLink(
    link: ZoomPersonalLink,
    meetingPassword: string,
    request: ZoomRedirectRequest = requestZoomRedirect,
): Promise<ResolvedZoomPersonalLink | ZoomPersonalLinkFailure> {
    let answer: { status: number; location: string | null };
    try {
        answer = await request(link.url);
    } catch (err) {
        return { failure: 'request-failed', detail: err instanceof Error ? `${err.name}: ${err.message}` : String(err) };
    }
    const meeting = answer.status >= 300 && answer.status < 400 ? meetingFromZoomRedirect(answer.location, link.url) : null;
    if (!meeting) return { failure: 'no-meeting', status: answer.status };
    const resolved: ResolvedZoomPersonalLink = {
        meetingId: meeting.meetingId,
        meetingPassword: joinPassword(meetingPassword, link.pwd || meeting.pwd),
    };
    return meeting.pwd ? { ...resolved, redirectPwd: meeting.pwd } : resolved;
}

/** The fields of a meeting invite a personal meeting link is resolved into. */
export interface ZoomInviteWithLink {
    meetingId: string;
    meetingPassword?: string;
    zoomPersonalLink?: ZoomPersonalLink;
}

/**
 * Resolve the invite's personal meeting link, if it has one, into its meeting
 * number and password, in place. True when the invite is ready to join with
 * (it had no personal link, or the link resolved); false when it did not
 * resolve, which the caller ends the join on. Only the host and the meeting
 * number are logged: the redirect carries the `pwd` value.
 */
export async function applyZoomPersonalLink(
    invite: ZoomInviteWithLink,
    request: ZoomRedirectRequest = requestZoomRedirect,
): Promise<boolean> {
    const link = invite.zoomPersonalLink;
    if (!link) return true;
    const result = await resolveZoomPersonalLink(link, invite.meetingPassword ?? '', request);
    if ('failure' in result) {
        const why =
            result.failure === 'request-failed'
                ? `the request failed (${result.detail})`
                : `Zoom answered HTTP ${result.status} without a redirect to a meeting`;
        log.warn(`[zoom] The personal meeting link on ${link.host} did not resolve: ${why}.`);
        return false;
    }
    if (result.redirectPwd) registerSecretValue(result.redirectPwd);
    registerSecretValue(result.meetingPassword);
    invite.meetingId = result.meetingId;
    invite.meetingPassword = result.meetingPassword || undefined;
    delete invite.zoomPersonalLink;
    log.info(`[zoom] The personal meeting link on ${link.host} resolved to meeting ${result.meetingId}.`);
    return true;
}

// What the Zoom web client shows for a meeting number it does not know, in
// the forms seen: the "Invalid meeting ID" error, error 3001 ("Meeting does
// not exist"), and the invalid-link page.
const ZOOM_MEETING_NOT_FOUND = [
    /\binvalid meeting id\b/i,
    /\bmeeting id is (?:not valid|invalid)\b/i,
    /\bmeeting does not exist\b/i,
    /\bmeeting link is (?:not valid|invalid)\b/i,
];

/** Whether the text of a Zoom page says the meeting does not exist. */
export function zoomSaysMeetingDoesNotExist(pageText: string | undefined | null): boolean {
    const text = (pageText ?? '').replace(/\s+/g, ' ');
    return ZOOM_MEETING_NOT_FOUND.some((pattern) => pattern.test(text));
}
