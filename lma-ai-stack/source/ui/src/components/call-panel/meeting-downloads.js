/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What a meeting can be taken away as, for the page header's Download menu.
 * The summary and the transcript are offered in the same formats -- Markdown,
 * Word, PDF, a web page (one self-contained .html file) and plain text, and
 * Excel for the transcript, which is a table -- and each can be copied to the
 * clipboard as Markdown. The summary's Word, PDF and web page files hold
 * thumbnails of the key frames it names. Every summary format holds every
 * section of the summary, in one reading order (downloadSummarySections). The
 * summary can also be sent as an email.
 */
import { ConsoleLogger } from 'aws-amplify/utils';

import { getEmailFormattedSummary, getTextFileFormattedMeetingDetails } from '../common/summary';
import {
  downloadSummaryAsDocx,
  downloadSummaryAsHtml,
  downloadSummaryAsPdf,
  downloadTranscriptAsDocx,
  downloadTranscriptAsExcel,
  downloadTranscriptAsHtml,
  downloadTranscriptAsMarkdown,
  downloadTranscriptAsPdf,
  downloadTranscriptAsText,
  exportToMarkdownFile,
  exportToTextFile,
  summaryDownloadMarkdown,
  transcriptDownloadMarkdown,
} from '../common/download-func';

const logger = new ConsoleLogger('MeetingDownloads');

const FORMAT_ITEMS = [
  { text: 'Markdown (.md)', id: 'markdown', iconName: 'file' },
  { text: 'Word (.docx)', id: 'docx', iconName: 'file' },
  { text: 'PDF (.pdf)', id: 'pdf', iconName: 'file' },
  { text: 'Web page (.html)', id: 'html', iconName: 'file' },
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
 * record, when it has one, and `participants` the names the page's header
 * lists, which the downloads write with the meeting's date and duration.
 * Resolves to whether a copy reached the clipboard, for 'copy', and to true
 * otherwise.
 */
export const runSummaryAction = async (id, item, consent = null, { participants = [] } = {}) => {
  const name = `Summary-${item.callId}`;
  const options = { participants };
  if (id === 'markdown') {
    await exportToMarkdownFile(summaryDownloadMarkdown(item, consent, options), name);
  } else if (id === 'docx') {
    await downloadSummaryAsDocx(item, consent, options);
  } else if (id === 'pdf') {
    await downloadSummaryAsPdf(item, consent, options);
  } else if (id === 'html') {
    await downloadSummaryAsHtml(item, consent, options);
  } else if (id === 'text') {
    await exportToTextFile(getTextFileFormattedMeetingDetails(item, consent, options), name);
  } else if (id === 'copy') {
    return copyText(summaryDownloadMarkdown(item, consent, options));
  } else if (id === 'email') {
    // Both halves are encoded: a meeting name is free text, and
    // getEmailFormattedSummary returns an encoded body.
    const subject = encodeURIComponent(item.callId ?? '');
    window.open(`mailto:?subject=${subject}&body=${getEmailFormattedSummary(item.callSummaryText)}`);
  }
  return true;
};

/**
 * Runs one of TRANSCRIPT_DOWNLOAD_ITEMS for a meeting; resolves as
 * runSummaryAction does. `consent` is the meeting's consent record, which the
 * PDF and the web page carry at their end.
 */
export const runTranscriptDownload = async (id, callTranscriptPerCallId, item, consent = null) => {
  if (id === 'markdown') {
    await downloadTranscriptAsMarkdown(callTranscriptPerCallId, item);
  } else if (id === 'docx') {
    await downloadTranscriptAsDocx(callTranscriptPerCallId, item);
  } else if (id === 'pdf') {
    await downloadTranscriptAsPdf(callTranscriptPerCallId, item, consent);
  } else if (id === 'html') {
    await downloadTranscriptAsHtml(callTranscriptPerCallId, item, consent);
  } else if (id === 'text') {
    await downloadTranscriptAsText(callTranscriptPerCallId, item);
  } else if (id === 'excel') {
    await downloadTranscriptAsExcel(callTranscriptPerCallId, item);
  } else if (id === 'copy') {
    return copyText(transcriptDownloadMarkdown(callTranscriptPerCallId, item));
  }
  return true;
};
