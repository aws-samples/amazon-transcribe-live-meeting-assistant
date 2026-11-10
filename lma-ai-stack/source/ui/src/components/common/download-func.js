/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable indent */
import * as XLSX from 'xlsx';
import { canonicalChannel, isChatChannel, isSpeechChannel, REMOTE_CHANNEL } from './channels';
import { parseSpeakerIdentities, resolveSpeakerName, speakerGroups } from './speaker-identities';
import { keyframeImagesAsText, summarySections } from './summary';
import { consentAsText } from './meeting-consent';
import { meetingTitle } from './meeting-header';

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
 * "<sender> (chat)", on one line.
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
      return { startTimestamp, endTimestamp, speaker, transcript };
    });

  return currentTurnByTurnSegments;
};

/** The transcript as the text and Word downloads write it: "Speaker [start]: text", a line per segment. */
export const transcriptDownloadText = (callTranscriptPerCallId, meeting) =>
  transcriptDownloadRows(callTranscriptPerCallId, meeting)
    .map((c) => `${c.speaker} [${c.startTimestamp}]: ${c.transcript}`)
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

/** The lines naming the meeting at the top of a Markdown download. */
const markdownMeetingFacts = (meeting) =>
  [
    `- **Meeting ID:** ${escapeMarkdown(meeting.callId)}`,
    meeting.initiationTimeStamp ? `- **Date:** ${meeting.initiationTimeStamp}` : null,
    meeting.conversationDurationTimeStamp ? `- **Duration:** ${meeting.conversationDurationTimeStamp}` : null,
  ]
    .filter(Boolean)
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
 * The summary as a Markdown document: the meeting's name and facts, a section
 * per summary section (a `##` heading each), and the consent record when the
 * meeting has one. A key frame cannot travel with the file, so its picture is
 * written as its alt text.
 */
export const summaryDownloadMarkdown = (meeting, consent = null) => {
  const parts = [`# ${escapeMarkdown(meetingTitle(meeting.callId))}`, markdownMeetingFacts(meeting)];
  const sections = summarySections(meeting.callSummaryText);
  if (!sections.length) parts.push('## Summary\n\nNot available');
  sections.forEach(({ title, markdown }) => {
    parts.push(`## ${title}\n\n${keyframeImagesAsText(markdown).trim()}`.trim());
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
  const lines = transcriptDownloadRows(callTranscriptPerCallId, meeting).map(
    (row) => `**${escapeMarkdown(row.speaker)}** [${row.startTimestamp}]: ${String(row.transcript ?? '').trim()}`,
  );
  const head = [`# Transcript: ${escapeMarkdown(meetingTitle(meeting.callId))}`, markdownMeetingFacts(meeting)];
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

/**
 * A Markdown document as the blocks of a Word document: headings (`#` to
 * `###`, deeper ones as `###`), bullets (their indent as the level, up to 3)
 * and paragraphs, each as runs of text. Kept apart from the docx library so it
 * can be tested directly.
 */
export const markdownToDocBlocks = (markdown) =>
  String(markdown ?? '')
    .split(/\r?\n/)
    .map((line) => {
      if (!line.trim()) return null;
      const heading = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (heading) return { kind: 'heading', level: Math.min(heading[1].length, 3), runs: inlineRuns(heading[2]) };
      const bullet = line.match(/^(\s*)[-*+]\s+(.*)$/);
      if (bullet) {
        return { kind: 'bullet', level: Math.min(Math.floor(bullet[1].length / 2), 3), runs: inlineRuns(bullet[2]) };
      }
      return { kind: 'paragraph', runs: inlineRuns(line.trim()) };
    })
    .filter(Boolean);

/** A Markdown document (the summary's) as a Word document, with its headings and bullets. */
export const exportMarkdownToDocxFile = async (markdown, nameFile) => {
  try {
    const docx = await import('docx');
    const headingLevels = [docx.HeadingLevel.HEADING_1, docx.HeadingLevel.HEADING_2, docx.HeadingLevel.HEADING_3];
    const children = markdownToDocBlocks(markdown).map((block) => {
      const runs = block.runs.map((run) => new docx.TextRun({ text: run.text, bold: run.bold }));
      if (block.kind === 'heading') {
        return new docx.Paragraph({ children: runs, heading: headingLevels[block.level - 1], spacing: { after: 160 } });
      }
      if (block.kind === 'bullet') {
        return new docx.Paragraph({ children: runs, bullet: { level: block.level }, spacing: { after: 60 } });
      }
      return new docx.Paragraph({ children: runs, spacing: { after: 120 } });
    });
    const doc = new docx.Document({ sections: [{ properties: {}, children }] });
    saveBlob(await docx.Packer.toBlob(doc), `${nameFile}.docx`);
  } catch (error) {
    console.error('Error exporting to DOCX:', error);
    alert('Could not generate DOCX file. Please make sure you have an internet connection and try again.');
  }
};

export const downloadTranscriptAsMarkdown = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  await exportToMarkdownFile(transcriptDownloadMarkdown(callTranscriptPerCallId, meeting), `Transcript-${callId}`);
};
