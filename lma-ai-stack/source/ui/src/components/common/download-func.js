/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable indent */
import * as XLSX from 'xlsx';
import { canonicalChannel, isChatChannel, isSpeechChannel, REMOTE_CHANNEL } from './channels';
import { parseSpeakerIdentities, resolveSpeakerName, speakerGroups } from './speaker-identities';
import { detectedLanguagesText, meetingLanguages, showsLanguageMarks } from './detected-languages';
import { downloadSummarySections, keyframeImagesAsText } from './summary';
import { consentAsText } from './meeting-consent';
import { meetingTitle } from './meeting-header';
import { fetchKeyframeThumbnails } from './keyframe-thumbnails';
import { buildPdf } from './pdf-export';

// eslint-disable-next-line prettier/prettier
export const onImportExcelAsync = (file) =>
  new Promise((resolve, reject) => {
    // Obtener el objeto del archivo cargado
    const { files } = file.target;
    // Leer el archivo a través del objeto FileReader

    const fileReader = new FileReader();
    fileReader.onload = (event) => {
      const { result } = event.target;
      // Leer en secuencia binaria para obtener todo el objeto de tabla de Excel
      const workbook = XLSX.read(result, { type: 'binary' });
      let data = []; // almacena los datos obtenidos
      // recorre cada hoja de trabajo para leer (aquí solo se lee la primera tabla por defecto)
      // eslint-disable-next-line no-restricted-syntax
      for (const sheet in workbook.Sheets) {
        // eslint-disable-next-line no-prototype-builtins
        if (workbook.Sheets.hasOwnProperty(sheet)) {
          // usa el método sheet_to_json para convertir Excel a datos json
          data = data.concat(XLSX.utils.sheet_to_json(workbook.Sheets[sheet]));
          // break; // Si solo se toma la primera tabla, descomenta esta línea
        }
      }
      resolve(data);

      // Aquí puede lanzar una solicitud relacionada para un error de tipo de archivo incorrecto
    };
    fileReader.onerror = reject;
    // Abre el archivo en modo binario
    fileReader.readAsBinaryString(files[0]);
  });

// A value as a cell. Text is written as a text cell, holding exactly the
// characters given: a value that starts with "=", "+", "-" or "@" (a chat
// message, a meeting name) is shown as typed, and is never a formula.
const asCell = (value) => (typeof value === 'string' ? { t: 's', v: value } : value);

/** The workbook exportToExcel writes: one sheet, a row per object in `data`. */
export const buildWorkbook = (data) => {
  const wb = XLSX.utils.book_new();
  const rows = data.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [key, asCell(value)])));
  const ws = XLSX.utils.json_to_sheet(rows, { origin: 'A2' });
  XLSX.utils.sheet_add_aoa(ws, []); // heading: array of arrays
  XLSX.utils.book_append_sheet(wb, ws);
  return wb;
};

export const exportToExcel = async (data, nameFile) => {
  if (data.length > 0) {
    XLSX.writeFile(buildWorkbook(data), `${nameFile}.xlsx`);
  }
};

// Hands a file to the browser to save.
const saveBlob = (blob, fileName) => {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
};

export const exportToTextFile = async (text, nameFile) => {
  saveBlob(new Blob([text], { type: 'text/plain' }), `${nameFile}.txt`);
};

export const exportToMarkdownFile = async (text, nameFile) => {
  saveBlob(new Blob([text], { type: 'text/markdown' }), `${nameFile}.md`);
};

/**
 * One line of the text transcript, "Speaker [time]: text", as
 * { speaker, timestamp, content }; null for a line of another shape.
 */
export const parseTranscriptLine = (line) => {
  const match = line.match(/^(.*?)\s+\[(.*?)\]:\s+(.*)$/);
  if (!match) return null;
  const [, speaker, timestamp, content] = match;
  return { speaker, timestamp, content };
};

// Function to export transcript as DOCX
export const exportToDocxFile = async (text, nameFile, title = `Meeting Transcript: ${nameFile}`) => {
  try {
    // Load the docx library dynamically
    const docx = await import('docx');

    // Parse the transcript text into structured data
    const lines = text.split('\n');
    const paragraphs = [];

    // Add title
    paragraphs.push(
      new docx.Paragraph({
        text: title,
        heading: docx.HeadingLevel.HEADING_1,
        spacing: { after: 200 },
      }),
    );

    // Process each line of the transcript
    lines.forEach((line) => {
      const parsed = parseTranscriptLine(line);
      if (parsed) {
        const { speaker, timestamp, content } = parsed;

        // Create a paragraph with different text runs for speaker, timestamp, and content
        paragraphs.push(
          new docx.Paragraph({
            children: [
              new docx.TextRun({
                text: speaker,
                bold: true,
              }),
              new docx.TextRun({
                text: ` [${timestamp}]: `,
                color: '666666',
              }),
              new docx.TextRun({
                text: content,
              }),
            ],
            spacing: { after: 120 },
          }),
        );
      } else if (line.trim()) {
        // For lines that don't match the pattern
        paragraphs.push(
          new docx.Paragraph({
            text: line,
            spacing: { after: 120 },
          }),
        );
      }
    });

    // Create the document
    const doc = new docx.Document({
      sections: [
        {
          properties: {},
          children: paragraphs,
        },
      ],
    });

    // Generate and download the document
    docx.Packer.toBlob(doc).then((blob) => saveBlob(blob, `${nameFile}.docx`));
  } catch (error) {
    console.error('Error exporting to DOCX:', error);

    // Fallback method if the docx library fails to load or process
    alert('Could not generate DOCX file. Please make sure you have an internet connection and try again.');
  }
};

const getTimestampFromSeconds = (secs) => {
  if (!secs || Number.isNaN(secs)) {
    return '00:00.0';
  }
  // Include the hours component once past an hour (substr(14,7) alone is
  // "MM:SS.s" and drops the hour — a 1h48m segment would print as "48:00.0").
  const iso = new Date(secs * 1000).toISOString();
  if (secs >= 3600) {
    return `${Math.floor(secs / 3600)}:${iso.substr(14, 5)}`; // H:MM:SS
  }
  return iso.substr(14, 7); // MM:SS.s
};

// A chat message on one line of a download: its line breaks become " / ".
const oneLine = (text) =>
  String(text ?? '')
    .trim()
    .replace(/\s*[\r\n]+\s*/g, ' / ');

/**
 * The transcript's segments as a download writes them, in time order:
 * [{ startTimestamp, endTimestamp, speaker, transcript }]. Every channel the
 * meeting has is included. A message typed in the meeting chat is under
 * "<sender> (chat)", on one line. In a meeting with more than one detected
 * language each row also carries `language`, the code of the language it was
 * identified in ('' when it names none), as the Transcript tab marks each line.
 */
export const transcriptDownloadRows = (callTranscriptPerCallId, meeting) => {
  const { callId, remoteParticipantName } = meeting;
  // An exported transcript has to carry the same speaker names the UI shows, or
  // a shared download contradicts the screen it was taken from.
  const speakerIdentities = parseSpeakerIdentities(meeting.speakerIdentities);

  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId);

  const allSegments = transcriptChannels
    .map((c) => {
      const { segments } = transcriptsForThisCallId[c];
      return segments;
    })
    // sort entries by end time
    .reduce((p, c) => [...p, ...c].sort((a, b) => a.endTime - b.endTime), []);
  // Whether each platform name's voices are one person or several, decided over
  // the whole transcript exactly as the Transcript tab decides it.
  const groups = speakerGroups(allSegments, speakerIdentities);
  const withLanguage = showsLanguageMarks(meetingLanguages(meeting.detectedLanguages, allSegments));

  const currentTurnByTurnSegments = allSegments
    // only extract the start time, end time, speaker and transcript
    .map((c) => {
      const { channel, startTime, endTime } = c;
      let { speaker, transcript } = c;

      if (isSpeechChannel(channel)) {
        // Resolve through the meeting's speaker identity map, which also covers
        // the older substitution of the participant name typed on the Stream
        // Audio form for the 'Other Participant' placeholder. That fallback is
        // passed for the REMOTE channel only -- it is the remote side's name, and
        // applying it to the local channel would mislabel it.
        speaker = resolveSpeakerName(
          c,
          speakerIdentities,
          canonicalChannel(channel) === REMOTE_CHANNEL ? remoteParticipantName : undefined,
          groups,
        );
      } else if (isChatChannel(channel)) {
        // Under the sender the meeting's chat gave, marked as chat, on one line.
        speaker = `${`${speaker ?? ''}`.trim() || 'Unknown sender'} (chat)`;
        transcript = oneLine(transcript);
      } else if (channel === 'AGENT_ASSISTANT' || channel === 'MEETING_ASSISTANT' || channel === 'CHAT_ASSISTANT') {
        // An assistant answer is under the assistant's name, whatever speaker it was stored with.
        speaker = 'MEETING ASSISTANT';

        // Clean up the transcript for the assistant to replace markdown and html
        const ASSISTANT_QUERY_STRING = '**Assistant Query:** *';
        const ASSISTANT_ANSWER_STRING = '*\n\n**Assistant Answer:**\n\n';
        transcript = transcript.replace(ASSISTANT_QUERY_STRING, '\n(Query): ');
        transcript = transcript.replace(ASSISTANT_ANSWER_STRING, '\n(Answer): ');

        const parsedTranscript = transcript.split('<details>');
        // eslint-disable-next-line prefer-const
        let [answer, sources] = parsedTranscript;
        // eslint-disable-next-line prefer-destructuring
        sources = sources?.split('</p></details>')[0] || '';

        sources = sources.replace('<summary>Context</summary><p style="white-space: pre-line;"><br>', '(Sources)\n');

        sources = sources.replaceAll(
          /<a href="([^"]+)">([^<]+)<\/a><br>([^<]+)[br>|^]/g,
          '- $2 \nLocation: $1\nQuote: $3',
        );
        sources = sources.replaceAll('<br>', '');

        transcript = answer + sources;
      }

      // modify start and end time from seconds to timestamp, e.g. 258.285 to 04:18.2
      const startTimestamp = getTimestampFromSeconds(startTime);
      const endTimestamp = getTimestampFromSeconds(endTime);
      if (withLanguage) {
        return { startTimestamp, endTimestamp, speaker, language: c.languageCode || '', transcript };
      }
      return { startTimestamp, endTimestamp, speaker, transcript };
    });

  return currentTurnByTurnSegments;
};

// The bracket after the speaker: the start time, and the row's language when
// the rows carry one ("04:18.2, es-US").
const rowStamp = (row) => (row.language ? `${row.startTimestamp}, ${row.language}` : row.startTimestamp);

/**
 * The transcript as the text and Word downloads write it: "Speaker [start]: text", a line per segment,
 * with the line's language after the time in a meeting with more than one ("Speaker [start, es-US]: text").
 */
export const transcriptDownloadText = (callTranscriptPerCallId, meeting) =>
  transcriptDownloadRows(callTranscriptPerCallId, meeting)
    .map((c) => `${c.speaker} [${rowStamp(c)}]: ${c.transcript}`)
    .join('\n');

export const downloadTranscriptAsExcel = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  const currentTurnByTurnSegments = transcriptDownloadRows(callTranscriptPerCallId, meeting);
  await exportToExcel(currentTurnByTurnSegments, `Transcript-${callId}`);
};

export const downloadTranscriptAsText = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  await exportToTextFile(transcriptDownloadText(callTranscriptPerCallId, meeting), `Transcript-${callId}`);
};

export const downloadTranscriptAsDocx = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  await exportToDocxFile(
    transcriptDownloadText(callTranscriptPerCallId, meeting),
    `Transcript-${callId}`,
    `Meeting Transcript: ${meetingTitle(callId)}`,
  );
};

// Characters that would start Markdown formatting in a name, escaped so the
// name reads as typed.
const escapeMarkdown = (text) => String(text ?? '').replace(/([\\`*_[\]<>#|])/g, '\\$1');

/**
 * The facts naming the meeting at the top of a download, [{ label, value }]:
 * its ID, date, participants (`participants`, the names the meeting page's
 * header lists, when the caller has them), duration, and detected languages.
 * The languages are listed only for a meeting in more than one, the same test
 * that marks each line with its language, so a meeting in one language
 * downloads as before. `segments` are the transcript's, when the download has
 * them: their languages are counted with the record's, as the per-line marks
 * count them.
 */
export const meetingFacts = (meeting, segments = [], participants = []) => {
  const languages = meetingLanguages(meeting.detectedLanguages, segments);
  const names = (participants || []).map((name) => String(name ?? '').trim()).filter(Boolean);
  return [
    { label: 'Meeting ID', value: String(meeting.callId ?? '') },
    meeting.initiationTimeStamp ? { label: 'Date', value: String(meeting.initiationTimeStamp) } : null,
    names.length ? { label: 'Participants', value: names.join(', ') } : null,
    meeting.conversationDurationTimeStamp
      ? { label: 'Duration', value: String(meeting.conversationDurationTimeStamp) }
      : null,
    showsLanguageMarks(languages) ? { label: 'Detected languages', value: detectedLanguagesText(languages) } : null,
  ].filter(Boolean);
};

/** The meeting's facts (meetingFacts) as the Markdown list at the top of a download. */
const markdownMeetingFacts = (meeting, segments = [], participants = []) =>
  meetingFacts(meeting, segments, participants)
    .map(({ label, value }) => `- **${label}:** ${escapeMarkdown(value)}`)
    .join('\n');

/** The consent record (consentAsText) as a Markdown section. */
export const consentAsMarkdown = (consent) => {
  const text = consentAsText(consent);
  if (!text) return '';
  const body = text
    .replace(/^CONSENT\n\n/, '')
    .split('\n')
    .map((line) => {
      // "Participants:" and the like open a list.
      if (/^[A-Z][A-Za-z ]*:$/.test(line)) return `\n**${line.slice(0, -1)}**\n`;
      const fact = line.match(/^([A-Z][A-Za-z ]*): (.*)$/);
      if (fact) return `- **${fact[1]}:** ${fact[2]}`;
      // The closing note, as a paragraph of its own.
      if (line && !line.startsWith('- ')) return `\n${line}`;
      return line;
    })
    .join('\n');
  return `## Consent\n\n${body}`.replace(/\n{3,}/g, '\n\n').trimEnd();
};

/**
 * The summary as a Markdown document: the meeting's name and facts, every
 * section of the summary (a `##` heading each) in reading order
 * (downloadSummarySections), and the consent record when the meeting has one.
 * A key frame cannot travel with a Markdown file, so its picture is written as
 * its alt text; with `keyframes: 'reference'` the picture's
 * `![alt](keyframe:<FrameId>)` reference is kept, for the Word and PDF
 * downloads, which embed the frame (summaryDocBlocks). `participants` are the
 * names listed with the meeting's facts.
 */
export const summaryDownloadMarkdown = (meeting, consent = null, { keyframes = 'text', participants = [] } = {}) => {
  const parts = [`# ${escapeMarkdown(meetingTitle(meeting.callId))}`, markdownMeetingFacts(meeting, [], participants)];
  const sections = downloadSummarySections(meeting.callSummaryText);
  if (!sections.length) parts.push('## Summary\n\nNot available');
  sections.forEach(({ title, markdown }) => {
    const body = keyframes === 'reference' ? markdown : keyframeImagesAsText(markdown);
    parts.push(`## ${title}\n\n${body.trim()}`.trim());
  });
  if (consent) parts.push(consentAsMarkdown(consent));
  return `${parts.join('\n\n')}\n`;
};

/**
 * The transcript as a Markdown document: the meeting's name and facts, then a
 * paragraph per segment, "**Speaker** [start]: text", with the same rows and
 * speaker names as the other transcript downloads (transcriptDownloadRows).
 */
export const transcriptDownloadMarkdown = (callTranscriptPerCallId, meeting) => {
  const rows = transcriptDownloadRows(callTranscriptPerCallId, meeting);
  const lines = rows.map(
    (row) => `**${escapeMarkdown(row.speaker)}** [${rowStamp(row)}]: ${String(row.transcript ?? '').trim()}`,
  );
  const head = [
    `# Transcript: ${escapeMarkdown(meetingTitle(meeting.callId))}`,
    markdownMeetingFacts(
      meeting,
      rows.map((row) => ({ languageCode: row.language })),
    ),
  ];
  return `${[...head, ...lines].join('\n\n')}\n`;
};

// Inline Markdown as runs of text: **bold** is bold, a link is its text and
// address, and the other markers (emphasis, code, escapes) are dropped.
// An escaped character is held as a private-use character while the markers
// are read, so `\*` is never taken for emphasis, and restored afterwards.
const ESCAPE_BASE = 0xe000;
const holdEscapes = (text) =>
  text.replace(/\\([\\`*_[\]<>#|])/g, (_m, c) => String.fromCharCode(ESCAPE_BASE + c.charCodeAt(0)));
const restoreEscapes = (text) =>
  text.replace(/[\ue000-\ue07f]/g, (c) => String.fromCharCode(c.charCodeAt(0) - ESCAPE_BASE));

const inlineRuns = (text) => {
  const plain = holdEscapes(text)
    .replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, label, url) => (label && label !== url ? `${label} (${url})` : url))
    .replace(/`([^`]*)`/g, '$1');
  return plain
    .split(/(\*\*[^*]+\*\*)/)
    .filter(Boolean)
    .map((part) => {
      const bold = part.length > 4 && part.startsWith('**') && part.endsWith('**');
      const inner = bold ? part.slice(2, -2) : part.replace(/(^|\W)[*_]([^*_\n]+)[*_](?=\W|$)/g, '$1$2');
      return { text: restoreEscapes(inner), bold };
    });
};

// A line that is only pictures of key frames, `![alt](keyframe:<FrameId>)`, as
// the screen share summary writes each one.
const KEYFRAME_REFERENCE = /!\[([^\]\n]*)\]\(keyframe:([A-Za-z0-9_-]{1,64})\)/g;
const keyframeLine = (line) => {
  const references = [...line.matchAll(KEYFRAME_REFERENCE)];
  if (!references.length || line.replace(KEYFRAME_REFERENCE, '').trim()) return null;
  return references.map(([, alt, id]) => ({ kind: 'keyframe', id, alt }));
};

/**
 * A Markdown document as the blocks of a Word document: headings (`#` to
 * `###`, deeper ones as `###`), bullets (their indent as the level, up to 3)
 * and paragraphs, each as runs of text. A line that is only a picture of a key
 * frame is a { kind: 'keyframe', id, alt } block, which summaryDocBlocks turns
 * into the picture or its text; a picture within other text is its alt text.
 * Kept apart from the docx library so it can be tested directly.
 */
export const markdownToDocBlocks = (markdown) =>
  String(markdown ?? '')
    .split(/\r?\n/)
    .flatMap((line) => {
      if (!line.trim()) return [];
      const frames = keyframeLine(line);
      if (frames) return frames;
      const text = keyframeImagesAsText(line);
      const heading = text.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (heading) return [{ kind: 'heading', level: Math.min(heading[1].length, 3), runs: inlineRuns(heading[2]) }];
      const bullet = text.match(/^(\s*)[-*+]\s+(.*)$/);
      if (bullet) {
        return [{ kind: 'bullet', level: Math.min(Math.floor(bullet[1].length / 2), 3), runs: inlineRuns(bullet[2]) }];
      }
      return [{ kind: 'paragraph', runs: inlineRuns(text.trim()) }];
    });

// A key frame that is not embedded, written as today's text description.
const keyframeAsText = (block) => ({ kind: 'paragraph', runs: inlineRuns(`(${block.alt})`) });

/**
 * The summary download (summaryDownloadMarkdown) as the blocks of a Word or
 * PDF document, with each key frame as a picture: { kind: 'image', id, alt,
 * image: { data, width, height } }, the thumbnail fetchKeyframeThumbnails
 * fetched. Up to MAX_EMBEDDED_KEYFRAMES frames are embedded; a frame past that,
 * or one that could not be fetched, is its alt text in brackets, as in the
 * other downloads.
 */
export const summaryDocBlocks = async (
  meeting,
  consent = null,
  { loadThumbnails = fetchKeyframeThumbnails, participants = [] } = {},
) => {
  const blocks = markdownToDocBlocks(
    summaryDownloadMarkdown(meeting, consent, { keyframes: 'reference', participants }),
  );
  const frameIds = blocks.filter((block) => block.kind === 'keyframe').map((block) => block.id);
  const thumbnails = frameIds.length && meeting.callId ? (await loadThumbnails(meeting.callId, frameIds)) || {} : {};
  return blocks.map((block) => {
    if (block.kind !== 'keyframe') return block;
    const image = thumbnails[block.id];
    return image ? { kind: 'image', id: block.id, alt: block.alt, image } : keyframeAsText(block);
  });
};

/** Blocks (markdownToDocBlocks, summaryDocBlocks) as the content of a Word document. */
export const docxChildren = (docx, blocks) => {
  const headingLevels = [docx.HeadingLevel.HEADING_1, docx.HeadingLevel.HEADING_2, docx.HeadingLevel.HEADING_3];
  return blocks.map((block) => {
    if (block.kind === 'image') {
      return new docx.Paragraph({
        children: [
          new docx.ImageRun({
            type: 'jpg',
            data: block.image.data,
            transformation: { width: block.image.width, height: block.image.height },
            altText: { name: block.alt || 'Shared screen', description: block.alt || 'Shared screen' },
          }),
        ],
        spacing: { after: 120 },
      });
    }
    if (block.kind === 'keyframe') return docxChildren(docx, [keyframeAsText(block)])[0];
    const runs = block.runs.map((run) => new docx.TextRun({ text: run.text, bold: run.bold, color: run.color }));
    if (block.kind === 'heading') {
      return new docx.Paragraph({ children: runs, heading: headingLevels[block.level - 1], spacing: { after: 160 } });
    }
    if (block.kind === 'bullet') {
      return new docx.Paragraph({ children: runs, bullet: { level: block.level }, spacing: { after: 60 } });
    }
    return new docx.Paragraph({ children: runs, spacing: { after: 120 } });
  });
};

/** Blocks as a Word document, saved as `<nameFile>.docx`. */
export const exportDocBlocksToDocxFile = async (blocks, nameFile) => {
  try {
    const docx = await import('docx');
    const doc = new docx.Document({ sections: [{ properties: {}, children: docxChildren(docx, blocks) }] });
    saveBlob(await docx.Packer.toBlob(doc), `${nameFile}.docx`);
  } catch (error) {
    console.error('Error exporting to DOCX:', error);
    alert('Could not generate DOCX file. Please make sure you have an internet connection and try again.');
  }
};

/**
 * Blocks as a PDF, saved as `<nameFile>.pdf` (see pdf-export.js). jsPDF is
 * loaded here, the first time a PDF is asked for, so it is not in the page's
 * bundle. `loadPdfLibrary` is for tests.
 */
export const exportBlocksToPdfFile = async (
  blocks,
  nameFile,
  title,
  { loadPdfLibrary = () => import('jspdf') } = {},
) => {
  try {
    const { jsPDF } = await loadPdfLibrary();
    const doc = await buildPdf(blocks, { jsPDF, title });
    saveBlob(doc.output('blob'), `${nameFile}.pdf`);
  } catch (error) {
    console.error('Error exporting to PDF:', error);
    alert('Could not generate PDF file. Please make sure you have an internet connection and try again.');
  }
};

/** The summary as a Word document, with thumbnails of its key frames. */
export const downloadSummaryAsDocx = async (meeting, consent = null, options = {}) => {
  await exportDocBlocksToDocxFile(await summaryDocBlocks(meeting, consent, options), `Summary-${meeting.callId}`);
};

/** The summary as a PDF: the Word document's content, thumbnails included. */
export const downloadSummaryAsPdf = async (meeting, consent = null, options = {}) => {
  await exportBlocksToPdfFile(
    await summaryDocBlocks(meeting, consent, options),
    `Summary-${meeting.callId}`,
    meetingTitle(meeting.callId),
    options,
  );
};

/**
 * The transcript as the blocks of a PDF: the Word transcript's title and its
 * segments, each "Speaker [start]: text" with the speaker in bold and the time
 * in grey, after the meeting's facts as the Markdown download lists them, and
 * the consent record at the end when the meeting has one.
 */
export const transcriptDocBlocks = (callTranscriptPerCallId, meeting, consent = null) => {
  const rows = transcriptDownloadRows(callTranscriptPerCallId, meeting);
  const head = markdownToDocBlocks(
    [
      `# Meeting Transcript: ${escapeMarkdown(meetingTitle(meeting.callId))}`,
      markdownMeetingFacts(
        meeting,
        rows.map((row) => ({ languageCode: row.language })),
      ),
    ].join('\n\n'),
  );
  const segments = rows.map((row) => ({
    kind: 'paragraph',
    runs: [
      { text: row.speaker, bold: true },
      { text: ` [${rowStamp(row)}]: `, color: '666666' },
      { text: String(row.transcript ?? '').trim() },
    ],
  }));
  const tail = consent ? markdownToDocBlocks(consentAsMarkdown(consent)) : [];
  return [...head, ...segments, ...tail];
};

/** The transcript as a PDF (transcriptDocBlocks). */
export const downloadTranscriptAsPdf = async (callTranscriptPerCallId, meeting, consent = null, options = {}) => {
  await exportBlocksToPdfFile(
    transcriptDocBlocks(callTranscriptPerCallId, meeting, consent),
    `Transcript-${meeting.callId}`,
    `Meeting Transcript: ${meetingTitle(meeting.callId)}`,
    options,
  );
};

export const downloadTranscriptAsMarkdown = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  await exportToMarkdownFile(transcriptDownloadMarkdown(callTranscriptPerCallId, meeting), `Transcript-${callId}`);
};

// The FrameIds the key frame pictures in a text name, in the order they appear.
const frameIdsInText = (text) => [...String(text ?? '').matchAll(KEYFRAME_REFERENCE)].map((match) => match[2]);

// The HTML renderer (html-export.jsx), loaded the first time a web page is
// asked for, so react-dom/server is not in the page's bundle.
const loadHtmlExport = () => import('./html-export');

// The consent record as the body of a section, without the heading the Markdown download gives it.
const consentBody = (consent) => (consent ? consentAsMarkdown(consent).replace(/^## Consent\n+/, '') : '');

/**
 * The summary as a self-contained HTML document (a string; see
 * html-export.jsx): the meeting's name and facts, every section of the summary
 * in reading order (downloadSummarySections), and the consent record, with
 * each key frame the summary pictures embedded from the thumbnail
 * fetchKeyframeThumbnails fetched (at most MAX_EMBEDDED_KEYFRAMES of them),
 * the same fetch the Word and PDF downloads use.
 */
export const summaryDownloadHtml = async (
  meeting,
  consent = null,
  { loadThumbnails = fetchKeyframeThumbnails, participants = [], loadRenderer = loadHtmlExport } = {},
) => {
  const found = downloadSummarySections(meeting.callSummaryText);
  const sections = found.length ? found : [{ title: 'Summary', markdown: 'Not available' }];
  const frameIds = sections.flatMap((section) => frameIdsInText(section.markdown));
  const thumbnails = frameIds.length && meeting.callId ? (await loadThumbnails(meeting.callId, frameIds)) || {} : {};
  const { renderSummaryHtml } = await loadRenderer();
  return renderSummaryHtml({
    title: meetingTitle(meeting.callId),
    facts: meetingFacts(meeting, [], participants),
    sections,
    consentMarkdown: consentBody(consent),
    thumbnails,
  });
};

/**
 * The transcript as a self-contained HTML document (a string): the same rows,
 * speaker names and times as the other transcript downloads
 * (transcriptDownloadRows), under the meeting's facts, and the consent record
 * at the end when the meeting has one.
 */
export const transcriptDownloadHtml = async (
  callTranscriptPerCallId,
  meeting,
  consent = null,
  { loadRenderer = loadHtmlExport } = {},
) => {
  const rows = transcriptDownloadRows(callTranscriptPerCallId, meeting);
  const { renderTranscriptHtml } = await loadRenderer();
  return renderTranscriptHtml({
    title: `Meeting Transcript: ${meetingTitle(meeting.callId)}`,
    facts: meetingFacts(
      meeting,
      rows.map((row) => ({ languageCode: row.language })),
    ),
    rows: rows.map((row) => ({
      speaker: row.speaker,
      stamp: rowStamp(row),
      text: String(row.transcript ?? '').trim(),
    })),
    consentMarkdown: consentBody(consent),
  });
};

const exportToHtmlFile = (html, nameFile) => {
  saveBlob(new Blob([html], { type: 'text/html;charset=utf-8' }), `${nameFile}.html`);
};

/** The summary as a web page, saved as `Summary-<callId>.html`. */
export const downloadSummaryAsHtml = async (meeting, consent = null, options = {}) => {
  try {
    exportToHtmlFile(await summaryDownloadHtml(meeting, consent, options), `Summary-${meeting.callId}`);
  } catch (error) {
    console.error('Error exporting to HTML:', error);
    alert('Could not generate the web page. Please make sure you have an internet connection and try again.');
  }
};

/** The transcript as a web page, saved as `Transcript-<callId>.html`. */
export const downloadTranscriptAsHtml = async (callTranscriptPerCallId, meeting, consent = null, options = {}) => {
  try {
    exportToHtmlFile(
      await transcriptDownloadHtml(callTranscriptPerCallId, meeting, consent, options),
      `Transcript-${meeting.callId}`,
    );
  } catch (error) {
    console.error('Error exporting to HTML:', error);
    alert('Could not generate the web page. Please make sure you have an internet connection and try again.');
  }
};
