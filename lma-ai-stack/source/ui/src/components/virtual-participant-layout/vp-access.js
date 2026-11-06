/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether the viewer may end, cancel or delete a Virtual Participant: they are
 * its owner or an Admin. The same rule is held by the backend (the "Virtual
 * Participant owner condition" of the AppSync templates, and caller_may_change
 * in the virtual_participant_manager Lambda); the page only follows it.
 */
export const canManageVirtualParticipant = (vp, { username, isAdmin } = {}) =>
  !!vp && (!!isAdmin || (!!username && vp.Owner === username));

/** Whether the viewer may delete every one of the selected Virtual Participants. */
export const canManageAll = (vps, viewer) =>
  vps.length > 0 && vps.every((vp) => canManageVirtualParticipant(vp, viewer));
