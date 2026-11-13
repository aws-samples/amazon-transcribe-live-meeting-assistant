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
