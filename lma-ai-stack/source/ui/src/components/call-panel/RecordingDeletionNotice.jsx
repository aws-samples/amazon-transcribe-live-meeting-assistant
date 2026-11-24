/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Says, above a meeting's transcript and player, that the deployment's
 * Retention settings delete its audio and video recordings after the summary:
 * when they are due to be deleted, or when they were. The deletion function
 * writes both times onto the meeting record (RecordingDeletionScheduledFor,
 * RecordingsDeletedAt). A pending deletion is shown only while the meeting has
 * a recording to download; a meeting with neither time shows nothing. See
 * docs/transcript-deletion.md.
 */
import React from 'react';
import { Alert } from '@cloudscape-design/components';

/* eslint-disable react/prop-types */

const formatTime = (iso) => {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
};

const RecordingDeletionNotice = ({ deletedAt, scheduledFor, filesDeleted = null, hasRecordings = false }) => {
  if (deletedAt) {
    // A meeting that was never recorded has nothing to say it lost.
    if (filesDeleted === 0 && !hasRecordings) return null;
    return (
      <Alert type="info" header="Recordings deleted">
        {`This meeting's audio and video recordings were deleted on ${formatTime(deletedAt)}, as this ` +
          "deployment's Retention settings require once a meeting is summarized, so they can no longer be " +
          'played or downloaded. The summary and the shared-screen slides are kept.'}
      </Alert>
    );
  }
  if (scheduledFor && hasRecordings) {
    return (
      <Alert type="warning" header="Recordings to be deleted">
        {`This meeting's audio and video recordings will be deleted at ${formatTime(scheduledFor)}, as this ` +
          "deployment's Retention settings require. Download them before then if you need a copy. " +
          'The summary and the shared-screen slides are kept.'}
      </Alert>
    );
  }
  return null;
};

export default RecordingDeletionNotice;
