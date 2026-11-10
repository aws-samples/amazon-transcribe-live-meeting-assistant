/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The speaker labels a client's control frames carry (START, SPEAKER_CHANGE,
 * END). See docs/websocket-streaming-api.md.
 *
 * The documented names are `localSpeakerName`, the speaker name for the
 * microphone channel (ch_1), and `remoteSpeakerName`, the speaker name for the
 * meeting-audio channel (ch_0). Clients written for LMA before 0.4.0 send the
 * same values as `agentId` and `fromNumber`, names LMA inherited from a
 * telephony solution, and those are still accepted. When a frame carries both
 * spellings of one label, the documented name wins.
 *
 * The rest of the server reads the labels from `agentId` and `fromNumber`, so
 * this copies a documented name onto its older field rather than the reverse.
 * `toNumber` has no newer name: nothing reads it, and it is stored only as the
 * meeting's `SystemPhoneNumber` attribute.
 */

/** The meeting-audio speaker name when a START names none (the UI's placeholder). */
export const DEFAULT_REMOTE_SPEAKER_NAME = 'Other Participant';
/** The value stored as SystemPhoneNumber when a START sends no `toNumber`. */
export const DEFAULT_SYSTEM_LABEL = 'System';

/** The documented name of each label, and the older field the server reads it from. */
export const SPEAKER_FIELD_ALIASES = {
    localSpeakerName: 'agentId',
    remoteSpeakerName: 'fromNumber',
} as const;

export type SpeakerFields = {
    agentId?: string;
    fromNumber?: string;
    localSpeakerName?: string;
    remoteSpeakerName?: string;
};

/**
 * Copy each documented speaker label onto the field the server reads.
 *
 * A documented name that is missing, empty or not a string leaves the older
 * field as the client sent it. Mutates and returns the frame.
 */
export const applySpeakerFieldAliases = <T extends SpeakerFields>(frame: T): T => {
    const aliases = Object.entries(SPEAKER_FIELD_ALIASES) as Array<
        [keyof typeof SPEAKER_FIELD_ALIASES, 'agentId' | 'fromNumber']
    >;
    for (const [name, older] of aliases) {
        const value: unknown = frame[name];
        if (typeof value === 'string' && value !== '') {
            frame[older] = value;
        }
    }
    return frame;
};
