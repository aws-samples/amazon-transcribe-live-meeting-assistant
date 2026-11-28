/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting pack and the email draft, without React: which files a meeting
 * can be sent or taken away as (the Summary PDF, the Slides PDF and the
 * Whiteboards PDF), why one is not available, how each is built in the
 * browser, the pack as one ZIP, and the email draft (.eml) with the chosen
 * files attached. Used by MeetingPackModal.jsx and the Download menu. See
 * docs/web-ui-guide.md#meeting-pack-and-email-participants.
 *
 * Every file is built from what the page already shows, read with the
 * viewer's own access: the summary from the meeting record, the slides from
 * getVideoKeyframes' signed links, the whiteboards from listMeetingWhiteboards
 * (kept current on the page by its subscription). Nothing is sent anywhere:
 * the files are saved to the viewer's computer.
 */
import { participantsInput, PARTICIPANT_EMAIL } from '../common/participant-list';
import { formatMeetingStart, meetingTitle } from '../common/meeting-header';
import {
  downloadSummarySections,
  getEmailFormattedSummary,
  getTextFileFormattedMeetingDetails,
} from '../common/summary';
import { meetingFacts, saveBlob, summaryPdfBlob } from '../common/download-func';
import { buildEml, encodedSize } from '../common/eml';
import { zipFiles } from '../common/zip-store';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';

export const PART_SUMMARY = 'summary';
export const PART_SLIDES = 'slides';
export const PART_WHITEBOARDS = 'whiteboards';

export const PACK_PARTS = [
  { id: PART_SUMMARY, label: 'Summary (PDF)', kind: 'Summary' },
  { id: PART_SLIDES, label: 'Slides (PDF)', kind: 'Slides' },
  { id: PART_WHITEBOARDS, label: 'Whiteboards (PDF)', kind: 'Whiteboards' },
];

export const NO_SLIDES = 'Nothing was shared on screen in this meeting.';
export const NO_DECK_FRAMES = 'The key frames are all of the camera or repeats, so there are no slides.';
export const NO_WHITEBOARDS = 'This meeting has no whiteboards.';
export const WHITEBOARDS_DELETED = "This meeting's transcript was deleted, and its whiteboards with it.";
export const LIVE_NOTE = 'This meeting is still going on: the files are a snapshot of it as it is now.';

/**
 * Mail services commonly refuse a message above 20 to 25 MB, measured once
 * encoded; above this the dialog suggests unticking the slides.
 */
export const EMAIL_SIZE_LIMIT_BYTES = 20 * 1024 * 1024;

/** An error saying a part has nothing to put in its file, with the reason. */
export class PartUnavailable extends Error {}

/**
 * Each part with whether the meeting has it and, when not, why:
 * [{ id, label, available, reason }]. `whiteboardsWritable` is false once the
 * transcript (and the whiteboards with it) has been deleted.
 */
export const packPartStates = ({
  hasSummary,
  summaryReason = 'This meeting has no summary yet.',
  keyframeCount = 0,
  whiteboardCount = 0,
  whiteboardsDeleted = false,
}) =>
  PACK_PARTS.map((part) => {
    if (part.id === PART_SUMMARY)
      return { ...part, available: Boolean(hasSummary), reason: hasSummary ? null : summaryReason };
    if (part.id === PART_SLIDES) {
      return { ...part, available: keyframeCount > 0, reason: keyframeCount > 0 ? null : NO_SLIDES };
    }
    let reason = null;
    if (!whiteboardCount) reason = whiteboardsDeleted ? WHITEBOARDS_DELETED : NO_WHITEBOARDS;
    return { ...part, available: whiteboardCount > 0, reason };
  });

/** What is ticked when the dialog opens: every part the meeting has. */
export const defaultSelection = (states) => Object.fromEntries(states.map((part) => [part.id, part.available]));

/** A name a file can be saved under on every system: no path separators, reserved or control characters. */
export const safeFileName = (name) =>
  String(name ?? '')
    // eslint-disable-next-line no-control-regex
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]+/g, '-')
    .replace(/\s+/g, ' ')
    .replace(/^[\s.-]+|[\s.]+$/g, '')
    .slice(0, 150) || 'meeting';

/** A part's file name, as the Download menu names the summary: `Slides-<meeting>.pdf`. */
export const partFileName = (id, callId) => {
  const part = PACK_PARTS.find((p) => p.id === id);
  return `${part ? part.kind : 'File'}-${safeFileName(callId)}.pdf`;
};

export const packZipName = (callId) => `Meeting-pack-${safeFileName(callId)}.zip`;
export const emlFileName = (callId) => `Email-${safeFileName(callId)}.eml`;

/** A size in bytes as "850 KB" or "12.4 MB". */
export const formatBytes = (bytes) => {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
};

/** Whether a message with attachments of `bytes` would be larger than mail services commonly take. */
export const emailTooLarge = (bytes) => encodedSize(bytes) > EMAIL_SIZE_LIMIT_BYTES;

/**
 * The people the email is addressed to: the meeting's participant list
 * (`item.participants`), each address once (compared ignoring case), with the
 * name given for it; an entry without a valid address is left out.
 */
export const emailRecipients = (participants) => {
  const seen = new Set();
  return participantsInput(participants)
    .map(({ name, email }) => ({ name: String(name || '').trim(), email: String(email || '').trim() }))
    .filter(({ email }) => {
      const key = email.toLowerCase();
      if (!PARTICIPANT_EMAIL.test(email) || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
};

/** The email's subject: the meeting's name and "summary". */
export const emailSubject = (callId) => `${meetingTitle(callId)} summary`;

/**
 * The mailto: link Open in mail app opens: the participants in To:, the
 * subject, and the summary as the body, as Email summary did.
 */
export const mailtoUrl = (item, recipients = emailRecipients(item.participants)) => {
  const to = recipients.map(({ email }) => encodeURIComponent(email).replace(/%40/g, '@')).join(',');
  const subject = encodeURIComponent(emailSubject(item.callId));
  return `mailto:${to}?subject=${subject}&body=${getEmailFormattedSummary(item.callSummaryText)}`;
};

/** Whether the meeting is still going on, so its files are a snapshot. */
export const isLive = (item) => item?.recordingStatusLabel === IN_PROGRESS_STATUS;

/** The facts a PDF cover lists: the meeting's date, owner, ID and duration. */
export const coverFacts = (item) =>
  [
    { label: 'Date', value: formatMeetingStart(item.initiationTimeStamp) },
    { label: 'Owner', value: item.owner || '' },
    { label: 'Meeting ID', value: String(item.callId ?? '') },
    item.conversationDurationTimeStamp ? { label: 'Duration', value: item.conversationDurationTimeStamp } : null,
  ].filter(Boolean);

const loadJsPdf = async () => (await import('jspdf')).jsPDF;
const loadRasterizer = async () => (await import('../common/text-raster')).browserTextRasterizer();

/**
 * How each part is built, as Blobs: replaced in tests. `ctx` holds what the
 * page has: { item, participants (the header's names), whiteboards,
 * chapters, loadFrames }.
 */
export const defaultBuilders = {
  // Without the consent record: these files are made to be shared with the
  // participants, and the record stays in Download › Summary › PDF (.pdf).
  [PART_SUMMARY]: async (ctx) => summaryPdfBlob(ctx.item, null, { participants: ctx.participants || [] }),
  [PART_SLIDES]: async (ctx, { onProgress, isCancelled }) => {
    const [{ loadEveryKeyframe }, { buildSlidesPdf, deckFrames }, jsPDF, rasterizeText] = await Promise.all([
      import('../common/slide-deck'),
      import('../common/slides-pdf'),
      loadJsPdf(),
      loadRasterizer(),
    ]);
    const frames = await loadEveryKeyframe(ctx.item.callId, ctx.loadFrames);
    if (!deckFrames(frames).length) throw new PartUnavailable(frames.length ? NO_DECK_FRAMES : NO_SLIDES);
    const doc = await buildSlidesPdf(
      {
        title: meetingTitle(ctx.item.callId),
        facts: coverFacts(ctx.item),
        note: isLive(ctx.item) ? LIVE_NOTE : '',
        frames,
        chapters: ctx.chapters || [],
      },
      { jsPDF, rasterizeText, onProgress, isCancelled },
    );
    return doc.output('blob');
  },
  [PART_WHITEBOARDS]: async (ctx, { onProgress, isCancelled }) => {
    const whiteboards = ctx.whiteboards || [];
    if (!whiteboards.length) throw new PartUnavailable(NO_WHITEBOARDS);
    const [{ buildWhiteboardsPdf }, jsPDF, rasterizeText] = await Promise.all([
      import('../meeting-whiteboards/whiteboard-pdf'),
      loadJsPdf(),
      loadRasterizer(),
    ]);
    const doc = await buildWhiteboardsPdf(
      {
        title: meetingTitle(ctx.item.callId),
        facts: coverFacts(ctx.item),
        note: isLive(ctx.item) ? LIVE_NOTE : '',
        whiteboards,
      },
      { jsPDF, rasterizeText, onProgress, isCancelled },
    );
    return doc.output('blob');
  },
};

/**
 * Builds one part: { id, blob, size, filename }. Rejects with PartUnavailable
 * when the part turns out to have nothing in it, and with the error otherwise.
 */
export const buildPart = async (
  id,
  ctx,
  { onProgress = () => {}, isCancelled = () => false, builders = defaultBuilders } = {},
) => {
  const blob = await builders[id](ctx, { onProgress, isCancelled });
  return { id, blob, size: blob.size, filename: partFileName(id, ctx.item.callId) };
};

const blobBytes = async (blob) => new Uint8Array(await blob.arrayBuffer());

/** The parts ([{ filename, blob }]) as one ZIP, a Blob. */
export const packZip = async (files) =>
  zipFiles(
    await Promise.all(files.map(async ({ filename, blob }) => ({ name: filename, data: await blobBytes(blob) }))),
  );

/** Saves the parts as one ZIP, or each as its own file (a short pause between them, as browsers ask). */
export const savePack = async (files, { callId, asZip = true, save = saveBlob, gapMs = 400 } = {}) => {
  if (asZip) {
    save(await packZip(files), packZipName(callId));
    return;
  }
  for (let i = 0; i < files.length; i += 1) {
    if (i > 0) {
      // eslint-disable-next-line no-await-in-loop -- one file after another
      await new Promise((resolve) => {
        setTimeout(resolve, gapMs);
      });
    }
    save(files[i].blob, files[i].filename);
  }
};

/**
 * The email draft (an RFC 5322 message, eml.js): To: the recipients, the
 * subject, the summary as text and as HTML, and the files attached.
 */
export const buildEmailDraft = async ({
  item,
  recipients,
  participants = [],
  files = [],
  date = new Date(),
  boundary,
  loadRenderer = () => import('../common/html-export'),
}) => {
  const { renderSummaryEmailHtml } = await loadRenderer();
  const found = downloadSummarySections(item.callSummaryText);
  const sections = found.length ? found : [{ title: 'Summary', markdown: 'Not available' }];
  const note = isLive(item) ? LIVE_NOTE : '';
  const html = renderSummaryEmailHtml({
    title: meetingTitle(item.callId),
    // The date as the meeting page's header writes it, in the sender's time zone.
    facts: meetingFacts(item, [], participants).map((fact) =>
      fact.label === 'Date' ? { ...fact, value: formatMeetingStart(item.initiationTimeStamp) || fact.value } : fact,
    ),
    sections,
    note,
    attachments: files.map((file) => file.filename),
  });
  const text = [note, getTextFileFormattedMeetingDetails(item, null, { participants })].filter(Boolean).join('\n\n');
  const attachments = await Promise.all(
    files.map(async ({ filename, blob }) => ({
      filename,
      contentType: 'application/pdf',
      data: await blobBytes(blob),
    })),
  );
  return buildEml({ to: recipients, subject: emailSubject(item.callId), text, html, attachments, date, boundary });
};
