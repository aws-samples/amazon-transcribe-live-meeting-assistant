// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Box, LineChart } from '@awsui/components-react';
import { Logger } from 'aws-amplify';

import { getWeightedSentimentLabel } from '../common/sentiment';

const logger = new Logger('SentimentCharts');

/* eslint-disable react/prop-types, react/destructuring-assignment */
export const VoiceToneFluctuationChart = ({ item, callTranscriptPerCallId }) => {
  const maxChannels = 6;
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId)
    .slice(0, maxChannels)
    .filter((c) => c !== 'AGENT_ASSISTANT')
    .filter((c) => c !== 'AGENT')
    .filter((c) => c !== 'CALLER')
    .filter((c) => c !== 'CATEGORY_MATCH');
  const sentimentPerChannel = transcriptChannels
    .map((channel) => transcriptsForThisCallId[channel])
    .map((transcript) =>
      // eslint-disable-next-line implicit-arrow-linebreak
      transcript.segments
        .filter((t) => t.sentimentWeighted)
        .reduce((p, c) => [...p, { x: new Date(c.endTime * 1000), y: c.sentimentWeighted }], [{ x: new Date(0), y: 0 }])
        .sort((a, b) => a.x - b.x),
    ); // eslint-disable-line function-paren-newline

  logger.debug('sentimentPerChannel', sentimentPerChannel);

  return (
    <LineChart
      height="180"
      hideFilter
      series={[
        {
          title: transcriptChannels[0] ? transcriptChannels[0].replace('_VOICETONE', '') : 'n/a',
          type: 'line',
          data: sentimentPerChannel[0] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
        {
          title: transcriptChannels[1] ? transcriptChannels[1].replace('_VOICETONE', '') : 'n/a',
          type: 'line',
          data: sentimentPerChannel[1] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
      ]}
      yDomain={[-1, 1]}
      i18nStrings={{
        legendAriaLabel: 'Legend',
        chartAriaRoleDescription: 'line chart',
        xTickFormatter: (e) => e.toISOString().substr(14, 5),
        // yTickFormatter: (e) => getWeightedSentimentLabel(e),
      }}
      empty={
        <Box textAlign="center" color="inherit">
          <b>No data available</b>
          <Box variant="p" color="inherit">
            There is no data available
          </Box>
        </Box>
      }
      statusType="finished"
      xScaleType="time"
      xTitle="Time"
      yTitle="Voice Tone Fluctuation"
    />
  );
};

/* eslint-disable react/prop-types, react/destructuring-assignment */
export const SentimentFluctuationChart = ({ item, callTranscriptPerCallId }) => {
  const maxChannels = 6;
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId)
    .slice(0, maxChannels)
    .filter((c) => c !== 'AGENT_ASSISTANT')
    .filter((c) => c !== 'AGENT_VOICETONE')
    .filter((c) => c !== 'CALLER_VOICETONE')
    .filter((c) => c !== 'CATEGORY_MATCH');

  const sentimentPerChannel = transcriptChannels
    .map((channel) => transcriptsForThisCallId[channel])
    .map((transcript) =>
      // eslint-disable-next-line implicit-arrow-linebreak
      transcript.segments
        .filter((t) => t.sentimentWeighted)
        .reduce((p, c) => [...p, { x: new Date(c.endTime * 1000), y: c.sentimentWeighted }], [{ x: new Date(0), y: 0 }])
        .sort((a, b) => a.x - b.x),
    ); // eslint-disable-line function-paren-newline

  logger.debug('sentimentPerChannel', sentimentPerChannel);

  return (
    <LineChart
      height="80"
      hideFilter
      series={[
        {
          title: transcriptChannels[0] || 'n/a',
          type: 'line',
          data: sentimentPerChannel[0] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
        {
          title: transcriptChannels[1] || 'n/a',
          type: 'line',
          data: sentimentPerChannel[1] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
      ]}
      yDomain={[-5, 5]}
      i18nStrings={{
        legendAriaLabel: 'Legend',
        chartAriaRoleDescription: 'line chart',
        xTickFormatter: (e) => e.toISOString().substr(14, 5),
        yTickFormatter: (e) => getWeightedSentimentLabel(e),
      }}
      empty={
        <Box textAlign="center" color="inherit">
          <b>No data available</b>
          <Box variant="p" color="inherit">
            There is no data available
          </Box>
        </Box>
      }
      statusType="finished"
      xScaleType="time"
      xTitle="Time"
      yTitle="Sentiment Fluctuation"
    />
  );
};

export const SentimentPerQuarterChart = ({ item, callTranscriptPerCallId }) => {
  const maxChannels = 6;
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId)
    .slice(0, maxChannels)
    .filter((c) => c !== 'AGENT_ASSISTANT')
    .filter((c) => c !== 'AGENT_VOICETONE')
    .filter((c) => c !== 'CALLER_VOICETONE')
    .filter((c) => c !== 'CATEGORY_MATCH');

  const sentimentByQuarterPerChannel = transcriptChannels
    .map((channel) => item?.sentiment?.SentimentByPeriod?.QUARTER[channel] || [])
    .map((sentimentByQuarter) =>
      // eslint-disable-next-line implicit-arrow-linebreak
      sentimentByQuarter
        .filter((s) => s.EndOffsetMillis > 0)
        .reduce((p, c) => [...p, { x: new Date(c.EndOffsetMillis), y: c.Score }], [{ x: new Date(0), y: 0 }]),
    ); // eslint-disable-line function-paren-newline

  logger.debug('sentimentByQuarterPerChannel', sentimentByQuarterPerChannel);

  return (
    <LineChart
      height="80"
      hideFilter
      series={[
        {
          title: transcriptChannels[0] || 'n/a',
          type: 'line',
          data: sentimentByQuarterPerChannel[0] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
        {
          title: transcriptChannels[1] || 'n/a',
          type: 'line',
          data: sentimentByQuarterPerChannel[1] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
      ]}
      yDomain={[-5, 5]}
      i18nStrings={{
        legendAriaLabel: 'Legend',
        chartAriaRoleDescription: 'line chart',
        xTickFormatter: (e) => e.toISOString().substr(14, 5),
        yTickFormatter: (e) => getWeightedSentimentLabel(e),
      }}
      empty={
        <Box textAlign="center" color="inherit">
          <b>No data available</b>
          <Box variant="p" color="inherit">
            There is no data available
          </Box>
        </Box>
      }
      statusType="finished"
      xScaleType="time"
      xTitle="Time"
      yTitle="Average Sentiment Per Quarter"
    />
  );
};
