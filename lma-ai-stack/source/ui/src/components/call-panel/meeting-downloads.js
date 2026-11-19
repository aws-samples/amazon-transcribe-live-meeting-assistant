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
 *
 * The menu (downloadMenuItems) has a group for each kind of file: Summary,
 * Transcript, Recordings (each session's audio and video), Screen share (the
 * slide deck, printed by the browser, and the key frames as a .zip), Chat
 * attachments (each file, and all of them as a .zip), and Everything (.zip).
 * An entry with nothing behind it is disabled with the reason. The .zip
 * archives are built on the server (use-meeting-archive.js); every file is
 * fetched through a time-limited signed link, after the same access check as
 * the meeting.
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
  { text: 'Word (.docx)', id: 'docx', iconName: 'file' },
  { text: 'PDF (.pdf)', id: 'pdf', iconName: 'file' },
  { text: 'Web page (.html)', id: 'html', iconName: 'file' },
  { text: 'Markdown (.md)', id: 'markdown', iconName: 'file' },
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

/** A meeting's recording links, one per session: the list a continued meeting keeps, then the current one. */
export const recordingSessions = (current, earlier) => {
  const urls = (Array.isArray(earlier) ? earlier : []).filter((url) => typeof url === 'string' && url);
  if (typeof current === 'string' && current && !urls.includes(current)) urls.push(current);
  return urls;
};

const NO_SHARED_SCREEN = 'Nothing was shared on screen in this meeting.';
const NO_ATTACHMENTS = 'No chat attachments were captured for this meeting.';

const recordingItems = (kind, label, urls) => {
  if (!urls.length) {
    return [
      {
        id: `recording:${kind}:0`,
        text: label,
        disabled: true,
        disabledReason: `This meeting has no ${kind} recording.`,
      },
    ];
  }
  return urls.map((_url, index) => ({
    id: `recording:${kind}:${index}`,
    text: urls.length > 1 ? `${label}, session ${index + 1}` : label,
    iconName: 'file',
  }));
};

/**
 * The Download menu's items, as nested groups. `meeting` holds what the page
 * knows: whether there is a summary (and why not), whether the transcript can
 * be downloaded (and why not), each session's recording links, how many key
 * frames the meeting has, and its stored chat attachments.
 */
export const downloadMenuItems = ({
  hasSummary,
  summaryReason,
  canDownloadTranscript,
  transcriptReason,
  audioUrls = [],
  videoUrls = [],
  keyframeCount = 0,
  attachments = [],
}) => {
  const stored = (attachments || []).filter((attachment) => attachment?.Status === 'STORED' && attachment.AttachmentId);
  return [
    {
      text: 'Summary',
      items: SUMMARY_ACTION_ITEMS.map((entry) => ({
        ...entry,
        id: `summary:${entry.id}`,
        ariaLabel: `Summary: ${entry.text}`,
        disabled: !hasSummary,
        disabledReason: hasSummary ? undefined : summaryReason,
      })),
    },
    {
      text: 'Transcript',
      items: TRANSCRIPT_DOWNLOAD_ITEMS.map((entry) => ({
        ...entry,
        id: `transcript:${entry.id}`,
        ariaLabel: `Transcript: ${entry.text}`,
        disabled: !canDownloadTranscript,
        disabledReason: canDownloadTranscript ? undefined : transcriptReason,
      })),
    },
    {
      text: 'Recordings',
      items: [...recordingItems('audio', 'Audio', audioUrls), ...recordingItems('video', 'Video', videoUrls)],
    },
    {
      text: 'Screen share',
      items: [
        { id: 'slides:deck', text: 'Slides (PDF)', iconName: 'file' },
        { id: 'archive:KEY_FRAMES', text: 'Key frames (.zip)', iconName: 'download' },
      ].map((entry) => ({
        ...entry,
        disabled: keyframeCount === 0,
        disabledReason: keyframeCount === 0 ? NO_SHARED_SCREEN : undefined,
      })),
    },
    {
      text: 'Chat attachments',
      items: [
        ...stored.map((attachment) => ({
          id: `attachment:${attachment.AttachmentId}`,
          text: attachment.FileName || 'Attachment',
          iconName: 'file',
        })),
        {
          id: 'archive:CHAT_ATTACHMENTS',
          text: 'All attachments (.zip)',
          iconName: 'download',
          disabled: !stored.length,
          disabledReason: stored.length ? undefined : NO_ATTACHMENTS,
        },
      ],
    },
    { id: 'archive:EVERYTHING', text: 'Everything (.zip)', iconName: 'download' },
  ];
};

/** A menu item id as its kind and the rest: 'attachment:a:b' is ['attachment', 'a:b']. */
export const parseMenuId = (id) => {
  const text = String(id ?? '');
  const at = text.indexOf(':');
  return at < 0 ? [text, ''] : [text.slice(0, at), text.slice(at + 1)];
};

/**
 * Opens a file whose link is signed on demand in a new tab. The tab is opened
 * within the click, before any await, because a browser blocks one opened
 * after; a link signed for download (an attachment, an archive) saves the
 * file rather than showing it. Resolves to whether the file was reached.
 */
export const openSignedFile = async (signLink, { open = window.open } = {}) => {
  const tab = open('', '_blank');
  if (tab) tab.opener = null;
  try {
    const url = await signLink();
    if (!url) throw new Error('no link');
    if (tab) tab.location.href = url;
    return true;
  } catch (error) {
    logger.error('could not open the file', error);
    if (tab) tab.close();
    return false;
  }
};
