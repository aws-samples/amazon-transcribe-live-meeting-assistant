/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting preparation a Virtual Participant carries onto the meeting it
 * records: labels, audience, meeting context, checklist, summary profile and
 * whether live guidance is on (docs/live-guidance.md) -- and its participant
 * list, the people expected at the meeting (docs/virtual-participant.md).
 *
 * Given when the VP is started or scheduled and stored on its own row, checked
 * there by the meeting preparation resolver. The VP reads them from that row
 * when it starts, with the agenda, and puts them in the START event, so the
 * meeting has them from its creation rather than from a second write racing
 * it -- the same route the agenda takes (meeting-agenda.ts). They are never
 * posted to the meeting chat or spoken. See docs/meeting-context-and-checklist.md.
 */

export interface CallLabel {
  Key: string;
  Value: string;
}

export interface ChecklistDefinition {
  ItemId: string;
  Text: string;
  Kind: string;
  Priority?: string;
  Source?: string;
}

export interface VpPreparation {
  labels: CallLabel[];
  audience: 'internal' | 'external' | null;
  meetingContext: string;
  checklist: ChecklistDefinition[];
  /** The summary profile the meeting is summarised with; null for the default. */
  summaryProfile: string | null;
  /** Who the meeting is shared with when it is created (the row's meetingSharedWith). */
  meetingSharedWith: string[];
  /** Live guidance on or off for the meeting; null for the deployment's setting. */
  guidanceEnabled?: boolean | null;
  /** The people expected at the meeting (the row's participants). */
  participants?: MeetingParticipant[];
}

export interface MeetingParticipant {
  name: string;
  email: string;
}

/** The bounds the meeting preparation resolver holds the row to. */
export const MAX_LABELS = 20;
export const MAX_CONTEXT_CHARS = 20000;
export const MAX_CHECKLIST_ITEMS = 100;

const isString = (value: unknown): value is string => typeof value === 'string';

/** A summary profile name, as the meeting preparation resolver checked it. */
const SUMMARY_PROFILE_NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** The meeting platforms by the names the meeting record gives them (Call.MeetingPlatform). */
const PLATFORM_NAMES: Record<string, string> = {
  CHIME: 'Chime',
  ZOOM: 'Zoom',
  TEAMS: 'Teams',
  WEBEX: 'Webex',
  GOOGLE_MEET: 'Google Meet',
};

/** The meeting platform as the meeting record names it, or null when it is not one LMA joins. */
export function meetingPlatformName(platform: unknown): string | null {
  if (!isString(platform)) return null;
  return PLATFORM_NAMES[platform.trim().toUpperCase().replace(/[\s-]+/g, '_')] ?? null;
}

/** The most people a meeting is shared with at creation, as the resolver allows. */
export const MAX_MEETING_SHARED_WITH = 50;

/** The most people a participant list holds, as the meeting preparation resolver allows. */
export const MAX_PARTICIPANTS = 100;

/**
 * The row's participant list, checked and normalized by the meeting preparation
 * resolver when it was stored; an entry without an email address is left out,
 * and an address is kept once, compared ignoring case.
 */
export function participantsFromVpRow(value: unknown): MeetingParticipant[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const participants: MeetingParticipant[] = [];
  for (const entry of value as any[]) {
    const email = isString(entry?.email) ? entry.email.trim() : '';
    if (!email || seen.has(email.toLowerCase())) continue;
    seen.add(email.toLowerCase());
    participants.push({ name: isString(entry?.name) ? entry.name : '', email });
  }
  return participants.slice(0, MAX_PARTICIPANTS);
}

/**
 * The entries of the row's meetingSharedWith, stored comma-separated by the
 * createVirtualParticipant and updateScheduledVirtualParticipant resolvers,
 * which refuse an entry holding "[", "]" or ",".
 */
function sharedWithEntries(value: unknown): string[] {
  if (!isString(value)) return [];
  const entries: string[] = [];
  for (const part of value.split(',')) {
    const entry = part.trim();
    if (entry && !entry.includes('[') && !entry.includes(']') && !entries.includes(entry)) entries.push(entry);
  }
  return entries.slice(0, MAX_MEETING_SHARED_WITH);
}

/**
 * The preparation stored on a VP row, or null when it has none.
 *
 * The row was checked when it was written; anything malformed in it anyway is
 * left out rather than failing the meeting, which is recorded either way.
 */
export function preparationFromVpRow(
  row:
    | {
        labels?: unknown;
        audience?: unknown;
        meetingContext?: unknown;
        checklist?: unknown;
        summaryProfile?: unknown;
        meetingSharedWith?: unknown;
        guidanceEnabled?: unknown;
        participants?: unknown;
      }
    | null
    | undefined,
): VpPreparation | null {
  const labels = (Array.isArray(row?.labels) ? row.labels : [])
    .filter((label: any) => label && isString(label.Key) && label.Key && isString(label.Value))
    .slice(0, MAX_LABELS)
    .map((label: any) => ({ Key: label.Key, Value: label.Value }));
  const audience = row?.audience === 'internal' || row?.audience === 'external' ? row.audience : null;
  const meetingContext = isString(row?.meetingContext) ? row.meetingContext.slice(0, MAX_CONTEXT_CHARS) : '';
  const checklist = (Array.isArray(row?.checklist) ? row.checklist : [])
    .filter((item: any) => item && isString(item.ItemId) && isString(item.Text) && isString(item.Kind))
    .slice(0, MAX_CHECKLIST_ITEMS)
    .map((item: any) => {
      const definition: ChecklistDefinition = { ItemId: item.ItemId, Text: item.Text, Kind: item.Kind };
      if (isString(item.Priority) && item.Priority) definition.Priority = item.Priority;
      if (isString(item.Source)) definition.Source = item.Source;
      return definition;
    });
  const meetingSharedWith = sharedWithEntries(row?.meetingSharedWith);
  const summaryProfile =
    isString(row?.summaryProfile) && row.summaryProfile !== 'default' && SUMMARY_PROFILE_NAME.test(row.summaryProfile)
      ? row.summaryProfile
      : null;
  const guidanceEnabled = typeof row?.guidanceEnabled === 'boolean' ? row.guidanceEnabled : null;
  const participants = participantsFromVpRow(row?.participants);
  if (
    !labels.length &&
    !audience &&
    !meetingContext.trim() &&
    !checklist.length &&
    !summaryProfile &&
    !meetingSharedWith.length &&
    guidanceEnabled === null &&
    !participants.length
  ) {
    return null;
  }
  return {
    labels,
    audience,
    meetingContext: meetingContext.trim() ? meetingContext : '',
    checklist,
    summaryProfile,
    meetingSharedWith,
    guidanceEnabled,
    participants,
  };
}

/**
 * The preparation with its participant list replaced by `participants`, the
 * list read from the row again just before the meeting is created. A VP that
 * started with no preparation gets one holding the list alone.
 */
export function withParticipants(
  preparation: VpPreparation | null,
  participants: MeetingParticipant[],
): VpPreparation {
  return {
    labels: [],
    audience: null,
    meetingContext: '',
    checklist: [],
    summaryProfile: null,
    meetingSharedWith: [],
    guidanceEnabled: null,
    ...(preparation ?? {}),
    participants,
  };
}

/**
 * The fields the START event adds for the preparation, named as CreateCallInput
 * names them. Empty when there is none, so a meeting without it is created
 * exactly as before. Every checklist item starts open.
 */
export function preparationStartFields(
  preparation: VpPreparation | null,
  updatedBy: string,
  now: Date = new Date(),
): Record<string, unknown> {
  if (!preparation) return {};
  const at = now.toISOString();
  const by = updatedBy || 'LMA';
  const fields: Record<string, unknown> = {};
  if (preparation.labels.length) fields.Labels = preparation.labels;
  if (preparation.audience) fields.Audience = preparation.audience;
  if (preparation.labels.length || preparation.audience) fields.MeetingMetadataUpdatedAt = at;
  if (preparation.meetingContext) {
    fields.MeetingContext = preparation.meetingContext;
    fields.MeetingContextUpdatedBy = by;
    fields.MeetingContextUpdatedAt = at;
  }
  if (preparation.checklist.length) {
    fields.Checklist = preparation.checklist.map((item) => ({
      ...item,
      Priority: item.Priority || 'should',
      Status: 'open',
      Evidence: [],
      UpdatedBy: by,
      UpdatedAt: at,
    }));
    fields.ChecklistUpdatedAt = at;
  }
  if (preparation.summaryProfile) fields.SummaryProfile = preparation.summaryProfile;
  // Left out for the deployment's setting, which the call event processor applies.
  if (typeof preparation.guidanceEnabled === 'boolean') fields.GuidanceEnabled = preparation.guidanceEnabled;
  // Only a VP started or scheduled with meetingSharedWith creates its meeting
  // shared; sharing the VP itself (its SharedWith) does not share the meeting.
  if (preparation.meetingSharedWith.length) fields.SharedWith = preparation.meetingSharedWith.join(',');
  // The list does not set SharedWith here: the meeting controls function
  // shares the meeting with it (Meeting Sharing) once the record exists.
  if (preparation.participants?.length) {
    fields.Participants = preparation.participants;
    fields.ParticipantsUpdatedBy = by;
    fields.ParticipantsUpdatedAt = at;
  }
  return fields;
}
