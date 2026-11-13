/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting password as the Virtual Participant enters it.
 *
 * Zoom invitations show a numeric passcode in groups ("1234 5678"), and Zoom
 * refuses it with the spaces. The invitation parser already removes them
 * (normalize_meeting_password in meeting_invitation_parser/index.py), but a
 * passcode typed into the create form, given to `lma vp create` or the SDK,
 * passed by the meeting assistant's start and schedule tools, or changed on a
 * scheduled Virtual Participant reaches the VP as it was written. Every one of
 * those paths ends here, where the platform is known, so the same rule is
 * applied once more: surrounding whitespace is removed from every password,
 * and for a Zoom passcode made only of digits and whitespace, the inner
 * whitespace too. Any other password keeps its inner characters, since only a
 * numeric Zoom passcode is known to be shown with readability spaces.
 */
export function normalizeMeetingPassword(password: string | undefined | null, platform: string | undefined): string {
    const cleaned = (password ?? '').trim();
    if ((platform ?? '').toUpperCase() === 'ZOOM' && /^[\d\s]+$/.test(cleaned)) {
        return cleaned.replace(/\s+/g, '');
    }
    return cleaned;
}

/**
 * The longest passcode Zoom lets a host set. Zoom's Meetings API reference
 * (developers.zoom.us/docs/api/meetings/, "Create a meeting", `password`)
 * says: "By default, a passcode can only have a maximum length of 10
 * characters and only contain alphanumeric characters and the @, -, _, and *
 * characters."
 */
export const ZOOM_PASSCODE_MAX_LENGTH = 10;

/** What a Zoom meeting password given to the Virtual Participant is. */
export type ZoomPasswordKind = 'passcode' | 'link-token';

/**
 * Whether a Zoom meeting password is a passcode, which is typed into the
 * passcode box on the join screen, or the `pwd=` value of a Zoom join link,
 * which is an encoded form of the passcode (commonly 32 characters ending in
 * ".1") and is only accepted on the join URL.
 *
 * A value is a link token when it is longer than {@link ZOOM_PASSCODE_MAX_LENGTH}
 * or ends in a period followed by digits, since a passcode cannot contain a
 * period. A truncated token is still longer than any passcode, so it is
 * classed as a token too. Anything else, including an empty value, is a
 * passcode. Expects the value {@link normalizeMeetingPassword} returns.
 */
export function zoomPasswordKind(password: string | undefined | null): ZoomPasswordKind {
    const value = (password ?? '').trim();
    if (value.length > ZOOM_PASSCODE_MAX_LENGTH || /\.\d+$/.test(value)) return 'link-token';
    return 'passcode';
}

/**
 * The Zoom web-client join URL. A link token goes on it as `pwd`, the way a
 * Zoom join link carries it; a passcode does not, because it is typed into
 * the join screen instead.
 */
export function zoomWebJoinUrl(meetingId: string, password: string | undefined | null): string {
    const base = `https://zoom.us/wc/${meetingId}/join`;
    const value = (password ?? '').trim();
    if (!value || zoomPasswordKind(value) !== 'link-token') return base;
    return `${base}?pwd=${encodeURIComponent(value)}`;
}
