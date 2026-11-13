/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { zoomPasswordKind } from './meeting-password.js';

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
 * value is not a Zoom link in one of the forms above. A personal link name
 * (`/my/<name>`) is not read: it does not carry the meeting number the web
 * client is opened with.
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
    const givenIsPasscode = !!meetingPassword && zoomPasswordKind(meetingPassword) === 'passcode';
    return {
        meetingId: link.meetingId,
        meetingPassword: givenIsPasscode ? meetingPassword : link.pwd || meetingPassword,
    };
}
