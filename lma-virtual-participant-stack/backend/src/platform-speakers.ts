/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which transcript speaker each platform participant id was last written
 * under, for linking a chat consent reply to the speaker who sent it
 * (meeting-consent.ts, SpeakerLink basis `platform_identity`).
 *
 * Only the joins whose platform gives the same stable id with the active
 * speaker and with a chat message's sender carry one: Teams through Azure
 * Communication Services and the Zoom Meeting SDK. The transcript writer
 * (kinesis-stream.ts) notes each final row of speech that carries an id; the
 * consent flow asks for the speaker of a chat sender's id. Nothing here
 * compares names.
 *
 * The ids are personal identifiers. They are held in memory for the meeting
 * only, and never logged.
 */

/** A transcript speaker as the meeting stores it. */
export interface PlatformSpeaker {
    channel: string;
    /** The speaker as stored, with the speech engine's voice label when it has one. */
    speaker: string;
}

/** The longest platform id kept; the consent API accepts no longer one. */
export const MAX_PLATFORM_ID_LENGTH = 256;

const latest = new Map<string, PlatformSpeaker>();
// Every id each stored speaker has been written with: a speaker label that two
// ids were written under (one voice label over two people) is nobody's.
const idsBySpeaker = new Map<string, Set<string>>();

const speakerKey = (channel: string, speaker: string): string => `${channel}\u0000${speaker}`;

/** A platform id as it is carried, or undefined when it is not one. */
export function platformId(value: unknown): string | undefined {
    if (typeof value !== 'string' && typeof value !== 'number') return undefined;
    const id = String(value).trim();
    return id && id.length <= MAX_PLATFORM_ID_LENGTH ? id : undefined;
}

/** A final row of speech was written under `speaker` on `channel`, with platform id `id`. */
export function notePlatformSpeaker(id: string | undefined, channel: string, speaker: string): void {
    const stored = speaker.trim();
    if (!id || !stored || !channel) return;
    latest.set(id, { channel, speaker: stored });
    const key = speakerKey(channel, stored);
    const ids = idsBySpeaker.get(key) ?? new Set<string>();
    ids.add(id);
    idsBySpeaker.set(key, ids);
}

/**
 * The speaker `id` was most recently written under, or undefined: when no row
 * of speech has carried it yet, or when that speaker's rows also carried
 * another participant's id.
 */
export function platformSpeakerFor(id: string | undefined): PlatformSpeaker | undefined {
    if (!id) return undefined;
    const found = latest.get(id);
    if (!found) return undefined;
    const ids = idsBySpeaker.get(speakerKey(found.channel, found.speaker));
    return ids && ids.size === 1 ? { ...found } : undefined;
}

/** Forgets every id: a new meeting, or a test. */
export function resetPlatformSpeakers(): void {
    latest.clear();
    idsBySpeaker.clear();
}
