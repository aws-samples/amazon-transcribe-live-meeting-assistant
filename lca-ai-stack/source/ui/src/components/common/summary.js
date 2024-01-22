// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

export const getTextOnlySummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonObj = JSON.parse(summary);
    if ('summary' in jsonObj) summary = jsonObj.summary;
    else if ('Summary' in jsonObj) summary = jsonObj.Summary;
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

export default getTextOnlySummary;
