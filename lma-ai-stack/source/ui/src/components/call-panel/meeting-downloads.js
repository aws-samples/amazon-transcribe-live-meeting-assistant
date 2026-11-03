/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What a meeting can be taken away as: the summary (copied, as a text file, as
 * a Word document or as an email) and the transcript (Excel, text or Word).
 * Shared by the Meeting Summary panel's own menu and the page header's
 * Download menu, so the two cannot drift.
 */
import { ConsoleLogger } from 'aws-amplify/utils';

import { getEmailFormattedSummary, getTextFileFormattedMeetingDetails } from '../common/summary';
import {
  downloadTranscriptAsDocx,
  downloadTranscriptAsExcel,
  downloadTranscriptAsText,
  exportToDocxFile,
  exportToTextFile,
} from '../common/download-func';

const logger = new ConsoleLogger('MeetingDownloads');

export const SUMMARY_ACTION_ITEMS = [
  { text: 'Copy to clipboard', id: 'copy', iconName: 'copy' },
  { text: 'Download summary', id: 'download', iconName: 'download' },
  { text: 'Email summary (beta)', id: 'email', iconName: 'envelope' },
  { text: 'Download as Word', id: 'docx', iconName: 'file' },
];

export const TRANSCRIPT_DOWNLOAD_ITEMS = [
  { text: 'Excel', id: 'excel' },
  { text: 'Text', id: 'text' },
  { text: 'Word', id: 'docx' },
];

const copyText = async (text) => {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    logger.error('Failed to copy to clipboard:', err);
    // Fallback for older browsers
    try {
      const textArea = document.createElement('textarea');
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand('copy');
      document.body.removeChild(textArea);
      return true;
    } catch (fallbackErr) {
      logger.error('Fallback copy failed:', fallbackErr);
      return false;
    }
  }
};

/** Runs one of SUMMARY_ACTION_ITEMS for a meeting; `consent` is its consent record, when it has one. */
export const runSummaryAction = async (id, item, consent = null) => {
  if (id === 'download') {
    await exportToTextFile(getTextFileFormattedMeetingDetails(item, consent), `Summary-${item.callId}`);
  } else if (id === 'email') {
    // Both halves are encoded: a meeting name is free text, and
    // getEmailFormattedSummary returns an encoded body.
    const subject = encodeURIComponent(item.callId ?? '');
    window.open(`mailto:?subject=${subject}&body=${getEmailFormattedSummary(item.callSummaryText)}`);
  } else if (id === 'docx') {
    await exportToDocxFile(getTextFileFormattedMeetingDetails(item, consent), `Summary-${item.callId}`);
  } else if (id === 'copy') {
    await copyText(getTextFileFormattedMeetingDetails(item, consent));
  }
};

/** Runs one of TRANSCRIPT_DOWNLOAD_ITEMS for a meeting. */
export const runTranscriptDownload = (id, callTranscriptPerCallId, item) => {
  if (id === 'text') {
    downloadTranscriptAsText(callTranscriptPerCallId, item);
  } else if (id === 'excel') {
    downloadTranscriptAsExcel(callTranscriptPerCallId, item);
  } else if (id === 'docx') {
    downloadTranscriptAsDocx(callTranscriptPerCallId, item);
  }
};
