/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The Create user form's "Meeting to invite them to": a Virtual Participant's
 * id or a meeting's CallId, sent as createUser's inviteToMeeting. The server
 * tries the id as a Virtual Participant's first, then as a meeting's. See
 * docs/user-management.md#inviting-a-new-user-to-a-meeting.
 */

/** How many days of meetings the form offers. */
export const INVITABLE_MEETING_DAYS = 14;

/** Virtual Participants that have ended or failed are not offered (their meeting, if any, is). */
const FINISHED_VP_STATUSES = new Set(['COMPLETED', 'FAILED', 'ENDED', 'CANCELLED']);

/**
 * The id named by what was typed or pasted: the id itself, or the id in a link
 * to a meeting page (#/calls/<CallId>), a Virtual Participant page
 * (#/virtual-participant/<id>) or a consent page (?meeting=<CallId> or ?vp=<id>).
 */
export const meetingRefFromText = (text) => {
  const value = (text || '').trim();
  if (!value) return '';
  const decode = (part) => {
    try {
      return decodeURIComponent(part);
    } catch (error) {
      return part;
    }
  };
  const query = value.match(/[?&](?:meeting|vp)=([^&#]+)/);
  if (query) return decode(query[1]).trim();
  const page = value.match(/#\/(?:calls|virtual-participant)\/([^/?#]+)/);
  if (page) return decode(page[1]).trim();
  return value;
};

/**
 * The form's options, as Autosuggest option groups: Virtual Participants still
 * to run or running, then the recent meetings, newest first. Each value is the
 * id sent; a meeting a listed Virtual Participant created is offered once, as
 * the Virtual Participant.
 */
export const invitableMeetingOptions = (vps, calls) => {
  const offered = (vps || [])
    .filter((vp) => vp?.id && !FINISHED_VP_STATUSES.has(vp.status))
    .sort((a, b) => String(b.scheduledFor || b.createdAt || '').localeCompare(a.scheduledFor || a.createdAt || ''));
  const vpOptions = offered.map((vp) => ({
    value: vp.id,
    label: vp.meetingName || vp.id,
    description: `Virtual Participant, ${String(vp.status || '').toLowerCase()}${
      vp.scheduledFor ? `, ${vp.scheduledFor}` : ''
    }`,
  }));
  const fromVps = new Set(offered.map((vp) => vp.CallId).filter(Boolean));
  const callOptions = (calls || [])
    .filter((call) => call?.CallId && !fromVps.has(call.CallId))
    .sort((a, b) => String(b.CreatedAt || '').localeCompare(a.CreatedAt || ''))
    .map((call) => ({
      value: call.CallId,
      label: call.CallId,
      description: `Meeting${call.CreatedAt ? `, ${call.CreatedAt}` : ''}`,
    }));
  const groups = [];
  if (vpOptions.length) groups.push({ label: 'Virtual Participants', options: vpOptions });
  if (callOptions.length) groups.push({ label: 'Recent meetings', options: callOptions });
  return groups;
};
