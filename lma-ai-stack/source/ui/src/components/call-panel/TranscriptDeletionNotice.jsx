/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Says, above a meeting's transcript, that the deployment's Retention settings
 * delete it after the summary: when it is due to be deleted, or when it was.
 * The transcript deletion function writes both times onto the meeting record
 * (TranscriptDeletionScheduledFor, TranscriptDeletedAt); a meeting with
 * neither shows nothing. See docs/transcript-deletion.md.
 */
import React from 'react';
import { Alert } from '@cloudscape-design/components';

/* eslint-disable react/prop-types */

const formatTime = (iso) => {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
};

const TranscriptDeletionNotice = ({ deletedAt, scheduledFor }) => {
  if (deletedAt) {
    return (
      <Alert type="info" header="Transcript deleted">
        {`This meeting's transcript was deleted on ${formatTime(deletedAt)}, as this deployment's ` +
          'Retention settings require once a meeting is summarized. The summary is kept.'}
      </Alert>
    );
  }
  if (scheduledFor) {
    return (
      <Alert type="warning" header="Transcript to be deleted">
        {`This meeting's transcript will be deleted at ${formatTime(scheduledFor)}, as this ` +
          "deployment's Retention settings require. Download it before then if you need a copy. " +
          'The summary is kept.'}
      </Alert>
    );
  }
  return null;
};

export default TranscriptDeletionNotice;
