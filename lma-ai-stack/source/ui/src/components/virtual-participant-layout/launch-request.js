/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The fields a "start now" launch passes to the Virtual Participant launcher
 * state machine. The meeting password is not one of them: it is saved on the
 * Virtual Participant's record by createVirtualParticipant, and the Virtual
 * Participant reads it from there when it starts.
 */
export const LAUNCH_FIELDS = [
  'meetingPlatform',
  'meetingID',
  'meetingName',
  'meetingTime',
  'userName',
  'virtualParticipantId',
  'userSub',
  'userZoomSub',
  'enableVideoRecording',
  'accessToken',
  'idToken',
  'rereshToken',
];

/**
 * The StartSyncExecution input for a launch: the listed fields that are given,
 * and nothing else. A field left undefined is left out, so the state machine's
 * own default for it applies.
 */
export const launchStateMachineInput = (fields) => {
  const data = {};
  LAUNCH_FIELDS.forEach((key) => {
    if (fields[key] !== undefined) data[key] = fields[key];
  });
  return { apiInfo: { httpMethod: 'POST' }, data };
};
