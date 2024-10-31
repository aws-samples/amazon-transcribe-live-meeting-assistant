// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import getRecordingStatus from './get-recording-status';
import { getSentimentTrendLabel, getWeightedSentimentLabel } from './sentiment';

/* Maps call attributes from API to a format that can be used in tables and panel */
// eslint-disable-next-line arrow-body-style
const mapCallsAttributes = (calls, settings) => {
  const regex = settings?.CategoryAlertRegex ?? '.*';
  const countAlerts = (categories) => categories.filter((category) => category.match(regex)).length;

  return calls.map((item) => {
    const {
      CallId: callId,
      AgentId: agentId,
      CallCategories: callCategories,
      IssuesDetected: issuesDetected,
      CallSummaryText: callSummaryText,
      CreatedAt: callTimestamp,
      CustomerPhoneNumber: callerPhoneNumber,
      SystemPhoneNumber: systemPhoneNumber,
      UpdatedAt: updatedAt,
      RecordingUrl: recordingUrl,
      TotalConversationDurationMillis: totalConversationDurationMillis = 0,
      Sentiment: sentiment = {},
      PcaUrl: pcaUrl,
      Owner: owner,
      SharedWith: sharedWith,
      ListPK: listPK,
      ListSK: listSK,
    } = item;
    const recordingStatus = getRecordingStatus(item);

    const callerAverageSentiment = sentiment?.OverallSentiment?.CALLER || 0;
    const callerSentimentLabel = getWeightedSentimentLabel(callerAverageSentiment);
    const agentAverageSentiment = sentiment?.OverallSentiment?.AGENT || 0;
    const agentSentimentLabel = getWeightedSentimentLabel(agentAverageSentiment);

    const callerSentimentByQuarter = sentiment?.SentimentByPeriod?.QUARTER?.CALLER || [];
    const callerSentimentTrendLabel = getSentimentTrendLabel(callerSentimentByQuarter);
    const agentSentimentByQuarter = sentiment?.SentimentByPeriod?.QUARTER?.AGENT || [];
    const agentSentimentTrendLabel = getSentimentTrendLabel(agentSentimentByQuarter);
    const callCategoryCount = callCategories?.length || 0;
    const alertCount = callCategories?.length ? countAlerts(callCategories) : 0;

    return {
      callId,
      agentId,
      callCategories,
      callCategoryCount,
      alertCount,
      issuesDetected,
      callSummaryText,
      callerPhoneNumber,
      systemPhoneNumber,
      updatedAt,
      recordingUrl,
      pcaUrl,
      totalConversationDurationMillis,
      conversationDurationTimeStamp: new Date(totalConversationDurationMillis).toISOString().substr(11, 8),
      sentiment,
      // change callTimestamp to a sortable date format
      initiationTimeStamp: new Date(callTimestamp).toISOString(),
      recordingStatusLabel: recordingStatus.label,
      recordingStatusIcon: recordingStatus.icon,
      callerAverageSentiment,
      callerSentimentLabel,
      callerSentimentTrendLabel,
      agentAverageSentiment,
      agentSentimentLabel,
      agentSentimentTrendLabel,
      owner,
      sharedWith,
      listPK,
      listSK,
    };
  });
};

export default mapCallsAttributes;
