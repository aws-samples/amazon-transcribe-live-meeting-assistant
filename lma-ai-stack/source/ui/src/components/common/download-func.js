/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
/* eslint-disable indent */
import * as XLSX from 'xlsx';
import { DEFAULT_OTHER_SPEAKER_NAME } from './constants';

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

export const exportToExcel = async (data, nameFile) => {
  if (data.length > 0) {
    const wb = XLSX.utils.book_new();

    const ws = XLSX.utils.json_to_sheet(data, { origin: 'A2' });
    XLSX.utils.sheet_add_aoa(ws, []); // heading: array of arrays

    XLSX.utils.book_append_sheet(wb, ws);

    XLSX.writeFile(wb, `${nameFile}.xlsx`);
  }
};

export const exportToTextFile = async (text, nameFile) => {
  const blob = new Blob([text], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${nameFile}.txt`;
  link.click();
  URL.revokeObjectURL(url);
};

// Function to export transcript as DOCX
export const exportToDocxFile = async (text, nameFile) => {
  try {
    // Load the docx library dynamically
    const docx = await import('docx');

    // Parse the transcript text into structured data
    const lines = text.split('\n');
    const paragraphs = [];

    // Add title
    paragraphs.push(
      new docx.Paragraph({
        text: `Meeting Transcript: ${nameFile}`,
        heading: docx.HeadingLevel.HEADING_1,
        spacing: { after: 200 },
      }),
    );

    // Process each line of the transcript
    lines.forEach((line) => {
      const match = line.match(/^(.*?)\s+\[(.*?)\]:\s+(.*)$/);
      if (match) {
        const [, speaker, timestamp, content] = match;

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
    docx.Packer.toBlob(doc).then((blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${nameFile}.docx`;
      link.click();
      URL.revokeObjectURL(url);
    });
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
  return new Date(secs * 1000).toISOString().substr(14, 7);
};

const sortTranscriptByTime = (callTranscriptPerCallId, meeting) => {
  const { callId, callerPhoneNumber } = meeting;

  const maxChannels = 6;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId).slice(0, maxChannels);

  const currentTurnByTurnSegments = transcriptChannels
    .map((c) => {
      const { segments } = transcriptsForThisCallId[c];
      return segments;
    })
    // sort entries by end time
    .reduce((p, c) => [...p, ...c].sort((a, b) => a.endTime - b.endTime), [])
    // only extract the start time, end time, speaker and transcript
    .map((c) => {
      const { channel, startTime, endTime } = c;
      let { speaker, transcript } = c;

      if (channel === 'CALLER' && (speaker === DEFAULT_OTHER_SPEAKER_NAME || speaker === '')) {
        // In streaming audio the speaker will just be "Other participant", override this with the
        // name the user chose if needed
        speaker = callerPhoneNumber || DEFAULT_OTHER_SPEAKER_NAME;
      } else if (channel === 'AGENT_ASSISTANT' || channel === 'MEETING_ASSISTANT') {
        // The speaker for the assistant will just be "Other participant", override this
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

export const downloadTranscriptAsExcel = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  const currentTurnByTurnSegments = sortTranscriptByTime(callTranscriptPerCallId, meeting);
  await exportToExcel(currentTurnByTurnSegments, `Transcript-${callId}`);
};

export const downloadTranscriptAsText = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  const currentTurnByTurnSegments = sortTranscriptByTime(callTranscriptPerCallId, meeting);

  // convert json to text lines of Speaker [start_timestamp]: transcript segment text
  const text = currentTurnByTurnSegments.map((c) => `${c.speaker} [${c.startTimestamp}]: ${c.transcript}`).join('\n');

  await exportToTextFile(text, `Transcript-${callId}`);
};

export const downloadTranscriptAsDocx = async (callTranscriptPerCallId, meeting) => {
  const { callId } = meeting;
  const currentTurnByTurnSegments = sortTranscriptByTime(callTranscriptPerCallId, meeting);

  // convert json to text lines of Speaker [start_timestamp]: transcript segment text
  const text = currentTurnByTurnSegments.map((c) => `${c.speaker} [${c.startTimestamp}]: ${c.transcript}`).join('\n');

  await exportToDocxFile(text, `Transcript-${callId}`);
};
