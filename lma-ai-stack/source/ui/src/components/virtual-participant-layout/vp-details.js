/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Virtual Participant detail page's view of its record, kept up to date
 * from the record as first read, the subscription's updates and the page's
 * quiet re-reads.
 */

/**
 * The fields an update leaves as they were when it carries no value for them.
 *
 * An update holds only what its writer selected: the Virtual Participant's own
 * status writes, for instance, select `Owner` but not `owner`, so their
 * subscription messages carry `owner: null`. Those are fields an update does
 * not clear, so a missing value keeps the one the page already has.
 */
const KEPT_WHEN_ABSENT = [
  'status',
  'updatedAt',
  'createdAt',
  'owner',
  'Owner',
  'meetingName',
  'meetingPlatform',
  'meetingId',
  'CallId',
  'vncEndpoint',
  'vncPort',
  'manualActionType',
  'manualActionMessage',
  'manualActionTimeoutSeconds',
  'manualActionStartTime',
  'joinMethod',
];

/** The statuses a run ends in; a rejoin sets the row back to INITIALIZING from one. */
const RUN_ENDED_STATUSES = ['ENDED', 'COMPLETED', 'FAILED'];

/** What describes one run of the Virtual Participant: its host's live view and how it ended. */
const RUN_FIELDS = [
  'vncEndpoint',
  'vncPort',
  'taskPrivateIp',
  'joinMethod',
  'errorMessage',
  'endTrigger',
  'endReason',
  'userAcknowledgedFailure',
];

/** The record after `updated`, merged over what the page held in `prev`. */
export const mergeVpUpdate = (prev, updated) => {
  // A rejoin starts a new run of the same row: the earlier run's live view and
  // outcome do not carry over, for anyone watching the page.
  if (RUN_ENDED_STATUSES.includes(prev?.status) && updated?.status === 'INITIALIZING') {
    const fresh = { ...prev, ...updated, vncReady: false };
    RUN_FIELDS.forEach((field) => {
      if (updated[field] == null) fresh[field] = null;
    });
    return fresh;
  }
  const next = { ...prev, ...updated };
  KEPT_WHEN_ABSENT.forEach((field) => {
    if (updated?.[field] == null && prev?.[field] != null) next[field] = prev[field];
  });
  // vncReady latches: once the viewer is ready, a later partial update does not unset it.
  if (prev?.vncReady === true) next.vncReady = true;
  else if (updated?.vncReady == null) next.vncReady = prev?.vncReady;
  return next;
};

/**
 * How the Virtual Participant joined, in words, from the record's
 * `joinMethod`; null when it is not recorded (other platforms, a VP that has
 * not been admitted yet, rows written before it was recorded). The container
 * writes `acs`, `browser`, or `browser:acs-refused:<subCode>` for Teams
 * (lma-virtual-participant-stack/backend/src/teams-join-method.ts).
 */
export const describeJoinMethod = (joinMethod) => {
  if (!joinMethod) return null;
  if (joinMethod === 'acs') return 'Azure Communication Services';
  if (joinMethod === 'browser') return 'Browser';
  const refused = /^browser:acs-refused:(\d+)$/.exec(joinMethod);
  if (refused) {
    return `Browser, after Teams refused the Azure Communication Services join (subCode ${refused[1]})`;
  }
  return joinMethod;
};

/**
 * Who owns the Virtual Participant: `Owner`, written when it is created and
 * the field the access rules read, else the older lowercase `owner`.
 */
export const vpOwner = (vp) => vp?.Owner || vp?.owner || '';
