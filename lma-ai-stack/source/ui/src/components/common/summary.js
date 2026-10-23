/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export const getTextOnlySummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonObj = JSON.parse(summary);
    // Do a case-insensitive search for 'summary' in the JSON object keys
    const summaryKey = Object.keys(jsonObj).find((key) => key.toLowerCase() === 'summary');
    if (summaryKey !== undefined) {
      summary = jsonObj[summaryKey];
    } else if (Object.keys(jsonObj).length > 0) {
      // If 'summary' is not found, use the first key as the summary
      summary = Object.keys(jsonObj)[0] || '';
      summary = jsonObj[summary];
    }
  } catch (e) {
    return callSummaryText;
  }
  return summary;
};

export const getMarkdownSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `**${key}**\n\n${value}\n\n`;
    });
  } catch (e) {
    return callSummaryText;
  }
  return summary;
};

/**
 * The summary as a percent-encoded `mailto:` body.
 *
 * The text is assembled as plain text and encoded in one pass at the end,
 * rather than written with `%0D%0A` escapes inline. A hand-edited summary is
 * plain Markdown and arrives here through the non-JSON branch, so the body
 * routinely contains characters that carry meaning in a URL -- a `#` opening a
 * heading, an `&` in a sentence -- and each has to reach the mail client as
 * itself rather than as a fragment or a parameter boundary.
 */
export const getEmailFormattedSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return encodeURIComponent('Not available');
  }
  let summary = callSummaryText;
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `${key}\n\n${value}\n\n`;
    });
  } catch (e) {
    summary = callSummaryText;
  }
  // CRLF is the line ending a mail body takes; normalise to LF first so a text
  // that already uses CRLF does not end up with doubled carriage returns.
  const body = summary.replace(/\*\*/g, '').replace(/\r\n/g, '\n').replace(/\n/g, '\r\n');
  return encodeURIComponent(body);
};

export const getTextFileFormattedSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `${key}\n\n${value}\n\n`;
      summary = summary.replace(/\*\*/g, '');
    });
  } catch (e) {
    return callSummaryText;
  }
  return summary;
};

export const getTextFileFormattedMeetingDetails = (meeting) => {
  let meetingDetails = '';
  meetingDetails += `Title: ${meeting.callId}\n`;

  // Not reformatting date in this iteration as it may change timezone from expected
  //  consider adding this in future iterations
  // const date = new Date(call.initiationTimeStamp);
  const date = meeting.initiationTimeStamp;
  meetingDetails += `Date: ${date}\n`;

  meetingDetails += `Length: ${meeting.conversationDurationTimeStamp}\n`;

  const summary = getTextFileFormattedSummary(meeting.callSummaryText);
  meetingDetails += `\n\n${summary}`;

  return meetingDetails;
};

export default getTextOnlySummary;
