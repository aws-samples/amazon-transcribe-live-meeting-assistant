// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

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

export const getEmailFormattedSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `${key}%0D%0A%0D%0A${value}%0D%0A%0D%0A`;
      summary = summary.replace(/\n/g, '%0D%0A');
      summary = summary.replace(/\*\*/g, '');
    });
  } catch (e) {
    return callSummaryText;
  }
  return summary;
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
