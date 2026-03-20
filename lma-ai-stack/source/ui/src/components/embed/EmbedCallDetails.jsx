/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedCallDetails - Embeddable call details with selectable sub-panels.
 * Renders only the requested panels (transcript, summary, chat) for a given callId.
 *
 * Query params:
 *   callId  - The meeting/call ID to load
 *   show    - Comma-separated panels: transcript, summary, chat
 *   layout  - Layout: vertical, horizontal, grid
 *
 * When show includes all panels or component=call-details, renders the full CallPanel.
 * When show is a subset (e.g., just 'chat'), renders only that specific panel.
 */
import React, { useEffect, useState, useRef, useCallback } from 'react';
import PropTypes from 'prop-types';
import { Logger } from 'aws-amplify';
import rehypeRaw from 'rehype-raw';
import ReactMarkdown from 'react-markdown';
import {
  Badge,
  Box,
  ColumnLayout,
  Container,
  Grid,
  Header,
  Popover,
  SpaceBetween,
  Spinner,
  Alert,
  TextContent,
  Toggle,
  Link,
} from '@awsui/components-react';

import useSettingsContext from '../../contexts/settings';
import { CallsContext } from '../../contexts/calls';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';
import mapCallsAttributes from '../common/map-call-attributes';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { CallPanel } from '../call-panel/CallPanel';
import { getMarkdownSummary } from '../common/summary';
import { COMPREHEND_PII_TYPES, DEFAULT_OTHER_SPEAKER_NAME, LANGUAGE_CODES } from '../common/constants';
import { SentimentIcon } from '../sentiment-icon/SentimentIcon';
import { getWeightedSentimentLabel } from '../common/sentiment';

const logger = new Logger('EmbedCallDetails');

const PAUSE_TO_MERGE_IN_SECONDS = 1;
const piiTypesSplitRegEx = new RegExp(`\\[(${COMPREHEND_PII_TYPES.join('|')})\\]`);

/**
 * Standalone summary panel - renders just the meeting summary.
 */
const EmbedSummaryPanel = ({ item }) => (
  <Container header={<Header variant="h4">Meeting Summary</Header>}>
    <TextContent color="gray">
      <ReactMarkdown rehypePlugins={[rehypeRaw]}>{getMarkdownSummary(item.callSummaryText)}</ReactMarkdown>
    </TextContent>
  </Container>
);

EmbedSummaryPanel.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
};

/**
 * Standalone chat panel - renders just the Meeting Assist Bot iframe.
 */
const EmbedChatPanel = ({ item }) => {
  const iframeSrc =
    process.env.REACT_APP_AGENT_ASSIST_MODE === 'LAMBDA'
      ? `/strands-chat.html?callId=${item.callId}`
      : `/index-lexwebui.html?callId=${item.callId}`;

  if (process.env.REACT_APP_ENABLE_AGENT_ASSIST !== 'true') {
    return (
      <Container header={<Header variant="h4">Meeting Assist Bot</Header>}>
        <Box textAlign="center" padding="l" color="text-body-secondary">
          Agent Assist is not enabled in this deployment.
        </Box>
      </Container>
    );
  }

  return (
    <Container
      disableContentPaddings
      header={
        <Header
          variant="h4"
          info={
            <Link variant="info" target="_blank" href="https://amazon.com/live-meeting-assistant">
              Info
            </Link>
          }
        >
          Meeting Assist Bot
        </Header>
      }
    >
      <div className="embed-chat-panel">
        <iframe
          style={{ border: '0px', height: '60vh', margin: '0' }}
          title="Meeting Assist"
          src={iframeSrc}
          width="100%"
        />
      </div>
    </Container>
  );
};

EmbedChatPanel.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
};

/* eslint-disable react/prop-types */

const getTimestampFromSeconds = (secs) => {
  if (!secs || Number.isNaN(secs)) {
    return '00:00.0';
  }
  return new Date(secs * 1000).toISOString().substr(14, 7);
};

const getSentimentImage = (segment, enableSentimentAnalysis) => {
  const { sentiment, sentimentScore, sentimentWeighted } = segment;
  if (!sentiment || !enableSentimentAnalysis) {
    return <div className="sentiment-image" />;
  }
  const weightedSentimentLabel = getWeightedSentimentLabel(sentimentWeighted);
  return (
    <Popover
      dismissAriaLabel="Close"
      header="Sentiment"
      size="medium"
      triggerType="custom"
      content={
        <SpaceBetween size="s">
          <div>
            <Box margin={{ bottom: 'xxxs' }} color="text-label">
              Sentiment
            </Box>
            <div>{sentiment}</div>
          </div>
          <div>
            <Box margin={{ bottom: 'xxxs' }} color="text-label">
              Sentiment Scores
            </Box>
            <div>{JSON.stringify(sentimentScore)}</div>
          </div>
          <div>
            <Box margin={{ bottom: 'xxxs' }} color="text-label">
              Weighted Sentiment
            </Box>
            <div>{sentimentWeighted}</div>
          </div>
        </SpaceBetween>
      }
    >
      <div className="sentiment-image-popover">
        <SentimentIcon sentiment={weightedSentimentLabel} />
      </div>
    </Popover>
  );
};

const TranscriptContent = ({ segment, translateCache }) => {
  const { settings } = useSettingsContext();
  const regex = settings?.CategoryAlertRegex ?? '.*';
  const { transcript, segmentId, channel, targetLanguage, translateOn } = segment;
  const k = segmentId.concat('-', targetLanguage);

  // prettier-ignore
  const currTranslated = translateOn
    && targetLanguage !== ''
    && translateCache[k] !== undefined
    && translateCache[k].translated !== undefined
    ? translateCache[k].translated
    : '';

  const result = currTranslated !== undefined ? currTranslated : '';
  const transcriptPiiSplit = transcript.split(piiTypesSplitRegEx);

  const transcriptComponents = transcriptPiiSplit.map((t, i) => {
    if (COMPREHEND_PII_TYPES.includes(t)) {
      // eslint-disable-next-line react/no-array-index-key
      return <Badge key={`${segmentId}-pii-${i}`} color="red">{`${t}`}</Badge>;
    }

    let className = '';
    let text = t;
    let translatedText = result;

    switch (channel) {
      case 'AGENT_ASSISTANT':
      case 'MEETING_ASSISTANT':
        className = 'transcript-segment-agent-assist';
        break;
      case 'AGENT':
      case 'CALLER':
        text = text.substring(text.indexOf(':') + 1).trim();
        translatedText = translatedText.substring(translatedText.indexOf(':') + 1).trim();
        break;
      case 'CATEGORY_MATCH':
        if (text.match(regex)) {
          className = 'transcript-segment-category-match-alert';
          text = `Alert: ${text}`;
        } else {
          className = 'transcript-segment-category-match';
          text = `Category: ${text}`;
        }
        break;
      default:
        break;
    }

    return (
      // eslint-disable-next-line react/no-array-index-key
      <TextContent key={`${segmentId}-text-${i}`} color="red" className={className}>
        <ReactMarkdown rehypePlugins={[rehypeRaw]}>{text.trim()}</ReactMarkdown>
        <ReactMarkdown className="translated-text" rehypePlugins={[rehypeRaw]}>
          {translatedText.trim()}
        </ReactMarkdown>
      </TextContent>
    );
  });

  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {transcriptComponents}
    </SpaceBetween>
  );
};

const TranscriptSegment = ({ segment, translateCache, enableSentimentAnalysis }) => {
  const { channel } = segment;

  if (channel === 'CATEGORY_MATCH') {
    const newSegment = { ...segment, transcript: `${segment.transcript}` };
    return (
      <Grid className="transcript-segment" disableGutters gridDefinition={[{ colspan: 1 }, { colspan: 10 }]}>
        {getSentimentImage(segment, enableSentimentAnalysis)}
        <SpaceBetween direction="vertical" size="xxs">
          <TranscriptContent segment={newSegment} translateCache={translateCache} />
        </SpaceBetween>
      </Grid>
    );
  }

  let displayChannel = `${segment.channel}`;
  let channelClass = '';

  if (channel === 'AGENT' || channel === 'CALLER') {
    displayChannel = `${segment.speaker}`.trim();
  } else if (channel === 'AGENT_ASSISTANT' || channel === 'MEETING_ASSISTANT') {
    displayChannel = 'MEETING_ASSISTANT';
    channelClass = 'transcript-segment-agent-assist';
  }

  return (
    <Grid className="transcript-segment" disableGutters gridDefinition={[{ colspan: 1 }, { colspan: 10 }]}>
      {getSentimentImage(segment, enableSentimentAnalysis)}
      <SpaceBetween direction="vertical" size="xxs" className={channelClass}>
        <SpaceBetween direction="horizontal" size="xs">
          <TextContent>
            <strong>{displayChannel}</strong>
          </TextContent>
          <TextContent>
            {`${getTimestampFromSeconds(segment.startTime)} - ${getTimestampFromSeconds(segment.endTime)}`}
          </TextContent>
        </SpaceBetween>
        <TranscriptContent segment={segment} translateCache={translateCache} />
      </SpaceBetween>
    </Grid>
  );
};

const shouldAppendToPreviousSegment = ({ previous, current }) =>
  // prettier-ignore
  previous.speaker === current.speaker
  && previous.channel === current.channel
  && current.startTime - previous.endTime < PAUSE_TO_MERGE_IN_SECONDS;

const appendToPreviousSegment = ({ previous, current }) => {
  /* eslint-disable no-param-reassign */
  previous.transcript += ` ${current.transcript}`;
  previous.endTime = current.endTime;
  previous.isPartial = current.isPartial;
};

/**
 * Standalone transcript panel - renders just the meeting transcript.
 */
const EmbedTranscriptPanel = ({ item, callTranscriptPerCallId }) => {
  const { settings } = useSettingsContext();
  const enableSentimentAnalysis = settings?.IsSentimentAnalysisEnabled === 'true';

  const bottomRef = useRef();
  const containerRef = useRef();
  const [autoScroll, setAutoScroll] = useState(item.recordingStatusLabel === IN_PROGRESS_STATUS);
  const [translateOn, setTranslateOn] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState(localStorage.getItem('targetLanguage') || '');
  const translateCache = {};
  const [userHasScrolled, setUserHasScrolled] = useState(false);

  const maxChannels = 6;
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId).slice(0, maxChannels);

  const handleLanguageSelect = (event) => {
    setTargetLanguage(event.target.value);
    localStorage.setItem('targetLanguage', event.target.value);
  };

  const getTurnByTurnSegments = useCallback(() => {
    const segments = transcriptChannels
      .map((c) => {
        const { segments: segs } = transcriptsForThisCallId[c];
        return segs;
      })
      .reduce((p, c) => [...p, ...c].sort((a, b) => a.endTime - b.endTime), [])
      .reduce((accumulator, current) => {
        if (
          // prettier-ignore
          !accumulator.length
          || !shouldAppendToPreviousSegment({ previous: accumulator[accumulator.length - 1], current })
          || translateOn
        ) {
          accumulator.push({ ...current });
        } else {
          appendToPreviousSegment({ previous: accumulator[accumulator.length - 1], current });
        }
        return accumulator;
      }, [])
      .map((c) => {
        const t = { ...c };
        t.targetLanguage = targetLanguage;
        t.translateOn = translateOn;
        if (t.speaker === DEFAULT_OTHER_SPEAKER_NAME || t.speaker === '') {
          t.speaker = item.callerPhoneNumber || DEFAULT_OTHER_SPEAKER_NAME;
        }
        return t;
      })
      .filter(
        (s) =>
          s?.segmentId &&
          s?.createdAt &&
          s.channel !== 'AGENT_VOICETONE' &&
          s.channel !== 'CALLER_VOICETONE' &&
          s.channel !== 'CHAT_ASSISTANT',
      )
      .map((s) => (
        <TranscriptSegment
          key={`${s.segmentId}-${s.createdAt}`}
          segment={s}
          translateCache={translateCache}
          enableSentimentAnalysis={enableSentimentAnalysis}
        />
      ));

    segments.push(<div key="bottom" ref={bottomRef} />);
    return segments;
  }, [callTranscriptPerCallId, targetLanguage, translateOn, translateCache]);

  const handleScroll = (e) => {
    const container = e.target;
    const threshold = 50;
    const isAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
    if (!isAtBottom && autoScroll) {
      setUserHasScrolled(true);
    } else if (isAtBottom) {
      setUserHasScrolled(false);
    }
  };

  const scrollToBottom = useCallback(() => {
    if (containerRef.current) {
      containerRef.current.scrollTo({ top: containerRef.current.scrollHeight, behavior: 'smooth' });
    }
  }, []);

  useEffect(() => {
    if (item.recordingStatusLabel === IN_PROGRESS_STATUS && autoScroll && !userHasScrolled && containerRef.current) {
      scrollToBottom();
    }
  }, [callTranscriptPerCallId, autoScroll, userHasScrolled, item.recordingStatusLabel]);

  const languageChoices = () => {
    if (translateOn) {
      return (
        <div>
          <select value={targetLanguage} onChange={handleLanguageSelect}>
            {LANGUAGE_CODES.map(({ value, label }) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
      );
    }
    return null;
  };

  return (
    <Container
      fitHeight="true"
      disableContentPaddings
      header={
        <Header
          variant="h4"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Toggle
                onChange={({ detail }) => setAutoScroll(detail.checked)}
                checked={autoScroll}
                disabled={item.recordingStatusLabel !== IN_PROGRESS_STATUS}
              />
              <span>Auto Scroll</span>
              <Toggle onChange={({ detail }) => setTranslateOn(detail.checked)} checked={translateOn} />
              <span>Enable Translation</span>
              {languageChoices()}
            </SpaceBetween>
          }
        >
          Meeting Transcript
        </Header>
      }
    >
      <div
        ref={containerRef}
        onScroll={handleScroll}
        style={{
          overflowY: 'auto',
          height: '68vh',
          maxHeight: '68vh',
          paddingLeft: '10px',
          paddingTop: '5px',
          paddingRight: '10px',
        }}
      >
        <ColumnLayout borders="horizontal" columns={1}>
          {getTurnByTurnSegments()}
        </ColumnLayout>
      </div>
    </Container>
  );
};

EmbedTranscriptPanel.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  callTranscriptPerCallId: PropTypes.object.isRequired,
};

/* eslint-enable react/prop-types */

/**
 * Standalone wrapper that provides CallsContext and renders
 * either the full CallPanel or individual sub-panels based on 'show'.
 */
const EmbedCallDetails = ({ params, sendToParent }) => {
  const { callId, show, layout } = params;
  const { settings } = useSettingsContext();

  const [call, setCall] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    sendGetTranscriptSegmentsRequest,
    setLiveTranscriptCallId,
    isCallsListLoading,
    setIsCallsListLoading,
    setPeriodsToLoad,
    periodsToLoad,
  } = useCallsGraphQlApi({ initialPeriodsToLoad: 0.5 });

  const [toolsOpen, setToolsOpen] = useState(false);
  const [selectedItems, setSelectedItems] = useState([]);

  // Determine which panels to show
  const showTranscript = show.includes('transcript');
  const showSummary = show.includes('summary');
  const showChat = show.includes('chat');
  const showAll = showTranscript && showSummary && showChat;

  // Fetch call details on mount
  useEffect(() => {
    if (!callId) {
      setError('No callId provided. Use ?callId=<meeting-id> to specify a meeting.');
      setLoading(false);
      return () => {};
    }

    const fetchCall = async () => {
      try {
        setLoading(true);
        const response = await getCallDetailsFromCallIds([callId]);
        logger.debug('Call detail response:', response);

        const callsMap = mapCallsAttributes(response, settings);
        const callDetails = callsMap[0];

        if (callDetails) {
          setCall(callDetails);
          if (!callTranscriptPerCallId[callId]) {
            await sendGetTranscriptSegmentsRequest(callId);
          }
          if (callDetails?.recordingStatusLabel === IN_PROGRESS_STATUS) {
            setLiveTranscriptCallId(callId);
          }

          sendToParent({
            type: 'LMA_CALL_LOADED',
            callId,
            status: callDetails.recordingStatusLabel,
          });
        } else {
          setError(`Meeting "${callId}" not found.`);
        }
      } catch (err) {
        logger.error('Error fetching call details:', err);
        setError('Failed to load meeting details. Please check the callId and try again.');
      } finally {
        setLoading(false);
      }
    };

    fetchCall();

    return () => {
      setLiveTranscriptCallId(null);
    };
  }, [callId]);

  // Update call when calls list updates (real-time)
  useEffect(() => {
    if (!callId || !call || !calls?.length) return;

    const callsFiltered = calls.filter((c) => c.CallId === callId);
    if (callsFiltered?.length) {
      const callsMap = mapCallsAttributes([callsFiltered[0]], settings);
      const callDetails = callsMap[0];
      if (callDetails?.updatedAt && call.updatedAt < callDetails.updatedAt) {
        setCall(callDetails);
      }
    }
  }, [calls, callId]);

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const callsContextValue = {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    selectedItems,
    sendGetTranscriptSegmentsRequest,
    setIsCallsListLoading,
    setLiveTranscriptCallId,
    setPeriodsToLoad,
    setToolsOpen,
    setSelectedItems,
    periodsToLoad,
    toolsOpen,
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box margin={{ top: 's' }}>Loading meeting details...</Box>
      </Box>
    );
  }

  if (error) {
    return (
      <Box padding="l">
        <Alert type="error" header="Error Loading Meeting">
          {error}
        </Alert>
      </Box>
    );
  }

  if (!call) {
    return (
      <Box padding="l">
        <Alert type="warning">Meeting not found</Alert>
      </Box>
    );
  }

  // If showing all panels, render the full CallPanel
  if (showAll) {
    return (
      <CallsContext.Provider value={callsContextValue}>
        <div className={`embed-layout-${layout}`}>
          <CallPanel
            item={call}
            setToolsOpen={setToolsOpen}
            callTranscriptPerCallId={callTranscriptPerCallId}
            getCallDetailsFromCallIds={getCallDetailsFromCallIds}
          />
        </div>
      </CallsContext.Provider>
    );
  }

  // Otherwise, render only the requested individual panels
  return (
    <CallsContext.Provider value={callsContextValue}>
      <div className={`embed-layout-${layout}`}>
        {showSummary && (
          <div className="embed-panel">
            <EmbedSummaryPanel item={call} />
          </div>
        )}

        {showTranscript && (
          <div className="embed-panel">
            <EmbedTranscriptPanel item={call} callTranscriptPerCallId={callTranscriptPerCallId} />
          </div>
        )}

        {showChat && (
          <div className="embed-panel">
            <EmbedChatPanel item={call} />
          </div>
        )}
      </div>
    </CallsContext.Provider>
  );
};

EmbedCallDetails.propTypes = {
  params: PropTypes.shape({
    callId: PropTypes.string,
    show: PropTypes.arrayOf(PropTypes.string),
    layout: PropTypes.string,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default EmbedCallDetails;
