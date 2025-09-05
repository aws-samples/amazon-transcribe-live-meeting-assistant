/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export const DONE_STATUS = 'Done';
export const IN_PROGRESS_STATUS = 'In Progress';
export const ERROR_STATUS = 'Error';

const getRecordingStatus = (item) => {
  const unknownStatus = { label: 'Unknown', icon: 'warning' };
  const errorStatus = { label: 'Error', icon: 'warning' };
  const inProgressStatus = { label: IN_PROGRESS_STATUS, icon: 'in-progress' };
  const doneStatus = { label: DONE_STATUS, icon: 'success' };

  const inProgressState = ['STARTED', 'TRANSCRIBING'];
  const doneState = ['ENDED'];
  const errorState = ['ERRORED'];

  if (inProgressState.includes(item.Status)) {
    return inProgressStatus;
  }
  if (doneState.includes(item.Status)) {
    return doneStatus;
  }
  if (errorState.includes(item.Status)) {
    return errorStatus;
  }

  return unknownStatus;
};

export default getRecordingStatus;
