/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Strands meeting-assist Lambda's request for an in-meeting request: a
 * question typed in the meeting chat (chat-ask.ts) or a lookup the voice
 * assistant makes for something spoken to it (nova-agent.ts,
 * elevenlabs-agent.ts).
 *
 * Every such request names its `toolScope`, and the meeting assistant decides
 * from the Meeting Assistant section's In-meeting requests setting
 * (`meeting_assist.inMeetingRequestTools`) what it is answered with: by
 * default the current meeting alone (its transcript and chat, agenda and
 * shared screen), with no user email; with "unrestricted", every tool, acting
 * for the LMA user sent as `userEmail`. The decision is the meeting
 * assistant's, read per answer, so a change to the setting reaches a Virtual
 * Participant already in a meeting.
 */

/** The in-meeting callers in this Virtual Participant, as the meeting assistant names them. */
export type InMeetingToolScope = 'meeting_chat' | 'voice_assistant';

export interface InMeetingRequestOptions {
    env?: Record<string, string | undefined>;
    /** The call id when neither VP_CALL_ID nor MEETING_NAME is set. */
    fallbackCallId: string;
    /** The user name when LMA_USER is not set. */
    fallbackUser?: string;
}

/** The meeting-assist Lambda's request for `text`, made in the meeting through `toolScope`. */
export function inMeetingRequestPayload(
    text: string,
    toolScope: InMeetingToolScope,
    { env = process.env, fallbackCallId, fallbackUser = '' }: InMeetingRequestOptions,
) {
    const callId = env.VP_CALL_ID || env.MEETING_NAME || fallbackCallId;
    return {
        text,
        call_id: callId,
        conversation_history: [],
        // Used only when the In-meeting requests setting is unrestricted.
        userEmail: env.LMA_USER || fallbackUser,
        toolScope,
        dynamodb_table_name: env.DYNAMODB_TABLE_NAME || '',
        dynamodb_pk: `c#${callId}`,
    };
}
