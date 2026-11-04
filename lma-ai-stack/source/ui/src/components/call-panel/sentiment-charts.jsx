/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React from 'react';
import { Box, LineChart } from '@cloudscape-design/components';
import { channelLabel, isSpeechChannel } from '../common/channels';
import { getWeightedSentimentLabel } from '../common/sentiment';

const logger = new ConsoleLogger('SentimentCharts');

const VOICETONE_CHANNELS = ['AGENT_VOICETONE', 'CALLER_VOICETONE'];

/* eslint-disable react/prop-types, react/destructuring-assignment */
export const VoiceToneFluctuationChart = ({ item, callTranscriptPerCallId }) => {
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  // The voice-tone readings, of every channel the meeting has.
  const transcriptChannels = Object.keys(transcriptsForThisCallId).filter((c) => VOICETONE_CHANNELS.includes(c));
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
          title: transcriptChannels[0] ? channelLabel(transcriptChannels[0].replace('_VOICETONE', '')) : 'n/a',
          type: 'line',
          data: sentimentPerChannel[0] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
        {
          title: transcriptChannels[1] ? channelLabel(transcriptChannels[1].replace('_VOICETONE', '')) : 'n/a',
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
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  // The speech channels, of every channel the meeting has: the assistants and
  // the meeting chat have no sentiment.
  const transcriptChannels = Object.keys(transcriptsForThisCallId).filter(isSpeechChannel);

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
          title: transcriptChannels[0] ? channelLabel(transcriptChannels[0]) : 'n/a',
          type: 'line',
          data: sentimentPerChannel[0] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
        {
          title: transcriptChannels[1] ? channelLabel(transcriptChannels[1]) : 'n/a',
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
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  // The speech channels, of every channel the meeting has: the assistants and
  // the meeting chat have no sentiment.
  const transcriptChannels = Object.keys(transcriptsForThisCallId).filter(isSpeechChannel);

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
          title: transcriptChannels[0] ? channelLabel(transcriptChannels[0]) : 'n/a',
          type: 'line',
          data: sentimentByQuarterPerChannel[0] || [],
          valueFormatter: (e) => e.toFixed(3),
        },
        {
          title: transcriptChannels[1] ? channelLabel(transcriptChannels[1]) : 'n/a',
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
