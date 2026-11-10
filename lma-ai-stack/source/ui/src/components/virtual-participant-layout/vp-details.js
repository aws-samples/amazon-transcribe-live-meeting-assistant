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
];

/** The record after `updated`, merged over what the page held in `prev`. */
export const mergeVpUpdate = (prev, updated) => {
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
 * Who owns the Virtual Participant: `Owner`, written when it is created and
 * the field the access rules read, else the older lowercase `owner`.
 */
export const vpOwner = (vp) => vp?.Owner || vp?.owner || '';
