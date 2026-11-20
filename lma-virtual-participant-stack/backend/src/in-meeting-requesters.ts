/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Who in the meeting may make a request to the meeting assistant: the Meeting
 * Assistant section's `inMeetingRequesters` setting, which stack-settings.ts
 * applies to `IN_MEETING_REQUESTERS` when the Virtual Participant starts.
 *
 * - `anyone` (the shipped default): every participant may type `LMA ask` and
 *   talk to the voice assistant.
 * - `owner_only`: only a request known to be the owner's. A Virtual Participant
 *   knows a chat sender only by the display name the meeting shows (and on the
 *   Teams browser join off the light-meetings client, not at all), and a speaker
 *   only by a diarization label, so no request it hears is known to be the
 *   owner's: it answers no question typed in the chat and does not start its
 *   voice assistant.
 * - `off`: no request made in the meeting is answered.
 *
 * The meeting assistant applies the same setting to every request it is sent
 * (strands_meeting_assist_function.in_meeting_request_refusal), so a Virtual
 * Participant started before a change is still held to it; this side only
 * saves the call and keeps the voice assistant out of the meeting.
 */

export type InMeetingRequesters = 'anyone' | 'owner_only' | 'off';

/**
 * The setting's shipped default, used when the configuration cannot be read.
 * Written as JSON (double quotes): lma-ai-stack/test/test_parameter_backed_settings.py
 * reads it and holds it to setting_defaults.json.
 */
export const IN_MEETING_REQUESTERS_DEFAULT: InMeetingRequesters = "anyone";

/** The environment variable stack-settings.ts sets from the setting. */
export const IN_MEETING_REQUESTERS_VARIABLE = 'IN_MEETING_REQUESTERS';

const VALUES: ReadonlySet<string> = new Set(['anyone', 'owner_only', 'off']);

/** The setting as applied; a value that is none of the three is read as `off`. */
export function inMeetingRequesters(env: Record<string, string | undefined> = process.env): InMeetingRequesters {
    const value = env[IN_MEETING_REQUESTERS_VARIABLE];
    if (value === undefined || value === '') return IN_MEETING_REQUESTERS_DEFAULT;
    return VALUES.has(value) ? (value as InMeetingRequesters) : 'off';
}

/**
 * Whether this Virtual Participant answers requests made in its meeting: only
 * when anyone may ask, since it cannot tell the owner from a guest.
 */
export function answersInMeetingRequests(env: Record<string, string | undefined> = process.env): boolean {
    return inMeetingRequesters(env) === 'anyone';
}

/** What `LMA ask` is told when requests made in the meeting are not answered. */
export function requestsNotAnsweredText(env: Record<string, string | undefined> = process.env): string {
    return inMeetingRequesters(env) === 'owner_only'
        ? 'only the meeting\'s owner can make requests here, and LMA cannot tell who is typing, so questions in the chat are not answered.'
        : 'questions in the chat are turned off on this deployment.';
}
