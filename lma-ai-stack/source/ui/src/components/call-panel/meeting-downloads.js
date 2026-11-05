/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What a meeting can be taken away as, for the page header's Download menu.
 * The summary and the transcript are offered in the same formats -- Markdown,
 * Word and plain text, and Excel for the transcript, which is a table -- and
 * each can be copied to the clipboard as Markdown. The summary can also be
 * sent as an email.
 */
import { ConsoleLogger } from 'aws-amplify/utils';

import { getEmailFormattedSummary, getTextFileFormattedMeetingDetails } from '../common/summary';
import {
  downloadTranscriptAsDocx,
  downloadTranscriptAsExcel,
  downloadTranscriptAsMarkdown,
  downloadTranscriptAsText,
  exportMarkdownToDocxFile,
  exportToMarkdownFile,
  exportToTextFile,
  summaryDownloadMarkdown,
  transcriptDownloadMarkdown,
} from '../common/download-func';

const logger = new ConsoleLogger('MeetingDownloads');

const FORMAT_ITEMS = [
  { text: 'Markdown (.md)', id: 'markdown', iconName: 'file' },
  { text: 'Word (.docx)', id: 'docx', iconName: 'file' },
  { text: 'Plain text (.txt)', id: 'text', iconName: 'file' },
];
const COPY_ITEM = { text: 'Copy to clipboard (Markdown)', id: 'copy', iconName: 'copy' };

export const SUMMARY_ACTION_ITEMS = [
  ...FORMAT_ITEMS,
  COPY_ITEM,
  { text: 'Email summary (beta)', id: 'email', iconName: 'envelope' },
];

export const TRANSCRIPT_DOWNLOAD_ITEMS = [
  ...FORMAT_ITEMS,
  { text: 'Excel (.xlsx)', id: 'excel', iconName: 'file' },
  COPY_ITEM,
];

/** Puts text on the clipboard; resolves to whether it got there. */
export const copyText = async (text) => {
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

/**
 * Runs one of SUMMARY_ACTION_ITEMS for a meeting; `consent` is its consent
 * record, when it has one. Resolves to whether a copy reached the clipboard,
 * for 'copy', and to true otherwise.
 */
export const runSummaryAction = async (id, item, consent = null) => {
  const name = `Summary-${item.callId}`;
  if (id === 'markdown') {
    await exportToMarkdownFile(summaryDownloadMarkdown(item, consent), name);
  } else if (id === 'docx') {
    await exportMarkdownToDocxFile(summaryDownloadMarkdown(item, consent), name);
  } else if (id === 'text') {
    await exportToTextFile(getTextFileFormattedMeetingDetails(item, consent), name);
  } else if (id === 'copy') {
    return copyText(summaryDownloadMarkdown(item, consent));
  } else if (id === 'email') {
    // Both halves are encoded: a meeting name is free text, and
    // getEmailFormattedSummary returns an encoded body.
    const subject = encodeURIComponent(item.callId ?? '');
    window.open(`mailto:?subject=${subject}&body=${getEmailFormattedSummary(item.callSummaryText)}`);
  }
  return true;
};

/** Runs one of TRANSCRIPT_DOWNLOAD_ITEMS for a meeting; resolves as runSummaryAction does. */
export const runTranscriptDownload = async (id, callTranscriptPerCallId, item) => {
  if (id === 'markdown') {
    await downloadTranscriptAsMarkdown(callTranscriptPerCallId, item);
  } else if (id === 'docx') {
    await downloadTranscriptAsDocx(callTranscriptPerCallId, item);
  } else if (id === 'text') {
    await downloadTranscriptAsText(callTranscriptPerCallId, item);
  } else if (id === 'excel') {
    await downloadTranscriptAsExcel(callTranscriptPerCallId, item);
  } else if (id === 'copy') {
    return copyText(transcriptDownloadMarkdown(callTranscriptPerCallId, item));
  }
  return true;
};
