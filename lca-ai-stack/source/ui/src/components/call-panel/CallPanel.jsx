// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useRef, useState } from 'react';
import {
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  Grid,
  Header,
  Link,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Tabs,
  TextContent,
  Toggle,
} from '@awsui/components-react';
import rehypeRaw from 'rehype-raw';
import ReactMarkdown from 'react-markdown';
import { TranslateClient, TranslateTextCommand } from '@aws-sdk/client-translate';
import { Logger } from 'aws-amplify';
import { StandardRetryStrategy } from '@aws-sdk/middleware-retry';
import { getMarkdownSummary } from '../common/summary';

import RecordingPlayer from '../recording-player';
import useSettingsContext from '../../contexts/settings';

import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { InfoLink } from '../common/info-link';
import { getWeightedSentimentLabel } from '../common/sentiment';

import {
  VoiceToneFluctuationChart,
  SentimentFluctuationChart,
  SentimentPerQuarterChart,
} from './sentiment-charts';

import './CallPanel.css';
import { SentimentTrendIcon } from '../sentiment-trend-icon/SentimentTrendIcon';
import { SentimentIcon } from '../sentiment-icon/SentimentIcon';
import useAppContext from '../../contexts/app';
import awsExports from '../../aws-exports';

const logger = new Logger('CallPanel');

// comprehend PII types
const piiTypes = [
  'BANK_ACCOUNT_NUMBER',
  'BANK_ROUTING',
  'CREDIT_DEBIT_NUMBER',
  'CREDIT_DEBIT_CVV',
  'CREDIT_DEBIT_EXPIRY',
  'PIN',
  'EMAIL',
  'ADDRESS',
  'NAME',
  'PHONE',
  'SSN',
];
const piiTypesSplitRegEx = new RegExp(`\\[(${piiTypes.join('|')})\\]`);

const MAXIMUM_ATTEMPTS = 100;
const MAXIMUM_RETRY_DELAY = 1000;

const languageCodes = [
  { value: '', label: 'Choose a Language' },
  { value: 'af', label: 'Afrikaans' },
  { value: 'sq', label: 'Albanian' },
  { value: 'am', label: 'Amharic' },
  { value: 'ar', label: 'Arabic' },
  { value: 'hy', label: 'Armenian' },
  { value: 'az', label: 'Azerbaijani' },
  { value: 'bn', label: 'Bengali' },
  { value: 'bs', label: 'Bosnian' },
  { value: 'bg', label: 'Bulgarian' },
  { value: 'ca', label: 'Catalan' },
  { value: 'zh', label: 'Chinese (Simplified)' },
  { value: 'zh-TW', label: 'Chinese (Traditional)' },
  { value: 'hr', label: 'Croatian' },
  { value: 'cs', label: 'Czech' },
  { value: 'da', label: 'Danish' },
  { value: 'fa-AF', label: 'Dari' },
  { value: 'nl', label: 'Dutch' },
  { value: 'en', label: 'English' },
  { value: 'et', label: 'Estonian' },
  { value: 'fa', label: 'Farsi (Persian)' },
  { value: 'tl', label: 'Filipino, Tagalog' },
  { value: 'fi', label: 'Finnish' },
  { value: 'fr', label: 'French' },
  { value: 'fr-CA', label: 'French (Canada)' },
  { value: 'ka', label: 'Georgian' },
  { value: 'de', label: 'German' },
  { value: 'el', label: 'Greek' },
  { value: 'gu', label: 'Gujarati' },
  { value: 'ht', label: 'Haitian Creole' },
  { value: 'ha', label: 'Hausa' },
  { value: 'he', label: 'Hebrew' },
  { value: 'hi', label: 'Hindi' },
  { value: 'hu', label: 'Hungarian' },
  { value: 'is', label: 'Icelandic' },
  { value: 'id', label: 'Indonesian' },
  { value: 'ga', label: 'Irish' },
  { value: 'it', label: 'Italian' },
  { value: 'ja', label: 'Japanese' },
  { value: 'kn', label: 'Kannada' },
  { value: 'kk', label: 'Kazakh' },
  { value: 'ko', label: 'Korean' },
  { value: 'lv', label: 'Latvian' },
  { value: 'lt', label: 'Lithuanian' },
  { value: 'mk', label: 'Macedonian' },
  { value: 'ms', label: 'Malay' },
  { value: 'ml', label: 'Malayalam' },
  { value: 'mt', label: 'Maltese' },
  { value: 'mr', label: 'Marathi' },
  { value: 'mn', label: 'Mongolian' },
  { value: 'no', label: 'Norwegian (Bokmål)' },
  { value: 'ps', label: 'Pashto' },
  { value: 'pl', label: 'Polish' },
  { value: 'pt', label: 'Portuguese (Brazil)' },
  { value: 'pt-PT', label: 'Portuguese (Portugal)' },
  { value: 'pa', label: 'Punjabi' },
  { value: 'ro', label: 'Romanian' },
  { value: 'ru', label: 'Russian' },
  { value: 'sr', label: 'Serbian' },
  { value: 'si', label: 'Sinhala' },
  { value: 'sk', label: 'Slovak' },
  { value: 'sl', label: 'Slovenian' },
  { value: 'so', label: 'Somali' },
  { value: 'es', label: 'Spanish' },
  { value: 'es-MX', label: 'Spanish (Mexico)' },
  { value: 'sw', label: 'Swahili' },
  { value: 'sv', label: 'Swedish' },
  { value: 'ta', label: 'Tamil' },
  { value: 'te', label: 'Telugu' },
  { value: 'th', label: 'Thai' },
  { value: 'tr', label: 'Turkish' },
  { value: 'uk', label: 'Ukrainian' },
  { value: 'ur', label: 'Urdu' },
  { value: 'uz', label: 'Uzbek' },
  { value: 'vi', label: 'Vietnamese' },
  { value: 'cy', label: 'Welsh' },
];

/* eslint-disable react/prop-types, react/destructuring-assignment */
const CallAttributes = ({ item, setToolsOpen }) => (
  <Container
    header={
      <Header variant="h4" info={<InfoLink onFollow={() => setToolsOpen(true)} />}>
        Call Attributes
      </Header>
    }
  >
    <ColumnLayout columns={6} variant="text-grid">
      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Call ID</strong>
          </Box>
          <div>{item.callId}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Agent</strong>
          </Box>
          <div>{item.agentId}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Initiation Timestamp</strong>
          </Box>
          <div>{item.initiationTimeStamp}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Last Update Timestamp</strong>
          </Box>
          <div>{item.updatedAt}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Duration</strong>
          </Box>
          <div>{item.conversationDurationTimeStamp}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Caller Phone Number</strong>
          </Box>
          <div>{item.callerPhoneNumber}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>System Phone Number</strong>
          </Box>
          <div>{item.systemPhoneNumber}</div>
        </div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <div>
          <Box margin={{ bottom: 'xxxs' }} color="text-label">
            <strong>Status</strong>
          </Box>
          <StatusIndicator type={item.recordingStatusIcon}>
            {` ${item.recordingStatusLabel} `}
          </StatusIndicator>
        </div>
      </SpaceBetween>
      {item?.pcaUrl?.length && (
        <SpaceBetween size="xs">
          <div>
            <Box margin={{ bottom: 'xxxs' }} color="text-label">
              <strong>Post Call Analytics</strong>
            </Box>
            <Button
              variant="normal"
              href={item.pcaUrl}
              target="_blank"
              iconAlign="right"
              iconName="external"
            >
              Open in Post Call Analytics
            </Button>
          </div>
        </SpaceBetween>
      )}
      {item?.recordingUrl?.length && item?.recordingStatusLabel !== IN_PROGRESS_STATUS && (
        <SpaceBetween size="xs">
          <div>
            <Box margin={{ bottom: 'xxxs' }} color="text-label">
              <strong>Recording Audio</strong>
            </Box>
            <RecordingPlayer recordingUrl={item.recordingUrl} />
          </div>
        </SpaceBetween>
      )}
    </ColumnLayout>
  </Container>
);

const CallCategories = ({ item }) => {
  const { settings } = useSettingsContext();
  const regex = settings?.CategoryAlertRegex ?? '.*';

  const categories = item.callCategories || [];

  const categoryComponents = categories.map((t, i) => {
    const className = t.match(regex)
      ? 'transcript-segment-category-match-alert'
      : 'transcript-segment-category-match';

    return (
      /* eslint-disable-next-line react/no-array-index-key */
      <SpaceBetween size="xs" key={`call-category-${i}`}>
        <div>
          {/* eslint-disable-next-line react/no-array-index-key */}
          <TextContent key={`call-category-${i}`} className={className}>
            <ReactMarkdown rehypePlugins={[rehypeRaw]}>{t.trim()}</ReactMarkdown>
          </TextContent>
        </div>
      </SpaceBetween>
    );
  });

  return (
    <Container
      fitHeight="true"
      header={
        <Header
          variant="h4"
          info={
            <Link
              variant="info"
              target="_blank"
              href="https://docs.aws.amazon.com/transcribe/latest/dg/call-analytics-create-categories.html"
            >
              Info
            </Link>
          }
        >
          Call Categories
        </Header>
      }
    >
      <ColumnLayout columns={6} variant="text-grid">
        {categoryComponents}
      </ColumnLayout>
    </Container>
  );
};

// eslint-disable-next-line arrow-body-style
const CallSummary = ({ item }) => {
  return (
    <Container
      header={
        <Header
          variant="h4"
          info={
            <Link
              variant="info"
              target="_blank"
              href="https://docs.aws.amazon.com/transcribe/latest/dg/call-analytics-insights.html#call-analytics-insights-summarization"
            >
              Info
            </Link>
          }
        >
          Call Summary
        </Header>
      }
    >
      <Grid
        gridDefinition={[{ colspan: { default: 12, xs: 6 } }, { colspan: { default: 12, xs: 6 } }]}
      >
        <Tabs
          tabs={[
            {
              label: 'Transcript Summary',
              id: 'summary',
              content: (
                <div>
                  {/* eslint-disable-next-line react/no-array-index-key */}
                  <TextContent color="gray">
                    <ReactMarkdown rehypePlugins={[rehypeRaw]}>
                      {getMarkdownSummary(item.callSummaryText) ?? 'No summary available'}
                    </ReactMarkdown>
                  </TextContent>
                </div>
              ),
            },
          ]}
        />
        <Tabs
          tabs={[
            {
              label: 'Issues',
              id: 'issues',
              content: (
                <div>
                  {/* eslint-disable-next-line react/no-array-index-key */}
                  <TextContent color="gray" className="issue-detected">
                    <ReactMarkdown rehypePlugins={[rehypeRaw]}>
                      {item.issuesDetected ?? 'No issue detected'}
                    </ReactMarkdown>
                  </TextContent>
                </div>
              ),
            },
          ]}
        />
      </Grid>
    </Container>
  );
};

const getSentimentImage = (segment) => {
  const { sentiment, sentimentScore, sentimentWeighted } = segment;
  if (!sentiment) {
    // returns an empty div to maintain spacing
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

const getTimestampFromSeconds = (secs) => {
  if (!secs || Number.isNaN(secs)) {
    return '00:00.0';
  }
  return new Date(secs * 1000).toISOString().substr(14, 7);
};

const TranscriptContent = ({ segment, translateCache }) => {
  const { settings } = useSettingsContext();
  const regex = settings?.CategoryAlertRegex ?? '.*';

  const { transcript, segmentId, channel, targetLanguage, agentTranscript, translateOn } = segment;

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
    if (piiTypes.includes(t)) {
      // eslint-disable-next-line react/no-array-index-key
      return <Badge key={`${segmentId}-pii-${i}`} color="red">{`${t}`}</Badge>;
    }

    let className = '';
    let text = t;
    let translatedText = result;
    switch (channel) {
      case 'AGENT_ASSISTANT':
        className = 'transcript-segment-agent-assist';
        break;
      case 'AGENT':
        text = agentTranscript !== undefined && agentTranscript ? text : '';
        translatedText = agentTranscript !== undefined && agentTranscript ? translatedText : '';
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
      // prettier-ignore
      // eslint-disable-next-line react/no-array-index-key
      <TextContent key={`${segmentId}-text-${i}`} color="red" className={className}>
        <ReactMarkdown rehypePlugins={[rehypeRaw]}>{text.trim()}</ReactMarkdown>
        <ReactMarkdown className="translated-text" rehypePlugins={[rehypeRaw]}>{translatedText.trim()}</ReactMarkdown>
      </TextContent>
    );
  });

  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {transcriptComponents}
    </SpaceBetween>
  );
};

const TranscriptSegment = ({ segment, translateCache }) => {
  const { channel } = segment;

  if (channel === 'CATEGORY_MATCH') {
    const categoryText = `${segment.transcript}`;
    const newSegment = segment;
    newSegment.transcript = categoryText;
    // We will return a special version of the grid that's specifically only for category.
    return (
      <Grid
        className="transcript-segment"
        disableGutters
        gridDefinition={[{ colspan: 1 }, { colspan: 10 }]}
      >
        {getSentimentImage(segment)}
        <SpaceBetween direction="vertical" size="xxs">
          <TranscriptContent segment={newSegment} translateCache={translateCache} />
        </SpaceBetween>
      </Grid>
    );
  }

  const channelClass = channel === 'AGENT_ASSISTANT' ? 'transcript-segment-agent-assist' : '';
  return (
    <Grid
      className="transcript-segment"
      disableGutters
      gridDefinition={[{ colspan: 1 }, { colspan: 10 }]}
    >
      {getSentimentImage(segment)}
      <SpaceBetween direction="vertical" size="xxs" className={channelClass}>
        <SpaceBetween direction="horizontal" size="xs">
          <TextContent>
            <strong>{segment.channel}</strong>
          </TextContent>
          <TextContent>
            {`${getTimestampFromSeconds(segment.startTime)} -
              ${getTimestampFromSeconds(segment.endTime)}`}
          </TextContent>
        </SpaceBetween>
        <TranscriptContent segment={segment} translateCache={translateCache} />
      </SpaceBetween>
    </Grid>
  );
};

const CallInProgressTranscript = ({
  item,
  callTranscriptPerCallId,
  autoScroll,
  translateClient,
  targetLanguage,
  agentTranscript,
  translateOn,
  collapseSentiment,
}) => {
  const bottomRef = useRef();
  const [turnByTurnSegments, setTurnByTurnSegments] = useState([]);
  const [translateCache, setTranslateCache] = useState({});
  const [cacheSeen, setCacheSeen] = useState({});
  const [lastUpdated, setLastUpdated] = useState(Date.now());
  const [updateFlag, setUpdateFlag] = useState(false);

  // channels: AGENT, AGENT_ASSIST, CALLER, CATEGORY_MATCH,
  // AGENT_VOICETONE, CALLER_VOICETONE
  const maxChannels = 6;
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId).slice(0, maxChannels);

  const getSegments = () => {
    const currentTurnByTurnSegments = transcriptChannels
      .map((c) => {
        const { segments } = transcriptsForThisCallId[c];
        return segments;
      })
      // sort entries by end time
      .reduce((p, c) => [...p, ...c].sort((a, b) => a.endTime - b.endTime), [])
      .map((c) => {
        const t = c;
        return t;
      });

    return currentTurnByTurnSegments;
  };

  const updateTranslateCache = (seg) => {
    const promises = [];
    // prettier-ignore
    for (let i = 0; i < seg.length; i += 1) {
      const k = seg[i].segmentId.concat('-', targetLanguage);

      // prettier-ignore
      if (translateCache[k] === undefined) {
        // Now call translate API
        const params = {
          Text: seg[i].transcript,
          SourceLanguageCode: 'auto',
          TargetLanguageCode: targetLanguage,
        };
        const command = new TranslateTextCommand(params);

        logger.debug('Translate API being invoked for:', seg[i].transcript, targetLanguage);

        promises.push(
          translateClient.send(command).then(
            (data) => {
              const n = {};
              logger.debug('Translate API response:', seg[i].transcript, targetLanguage, data.TranslatedText);
              n[k] = { cacheId: k, transcript: seg[i].transcript, translated: data.TranslatedText };
              return n;
            },
            (error) => {
              logger.debug('Error from translate:', error);
            },
          ),
        );
      }
    }
    return promises;
  };

  // Translate all segments when the call is completed.
  useEffect(() => {
    if (translateOn && targetLanguage !== '' && item.recordingStatusLabel !== IN_PROGRESS_STATUS) {
      const promises = updateTranslateCache(getSegments());
      Promise.all(promises).then((results) => {
        // prettier-ignore
        if (results.length > 0) {
          setTranslateCache((state) => ({
            ...state,
            ...results.reduce((a, b) => ({ ...a, ...b })),
          }));
          setUpdateFlag((state) => !state);
        }
      });
    }
  }, [targetLanguage, agentTranscript, translateOn, item.recordingStatusLabel]);

  // Translate real-time segments when the call is in progress.
  useEffect(async () => {
    const c = getSegments();
    // prettier-ignore
    if (
      translateOn
      && targetLanguage !== ''
      && c.length > 0
      && item.recordingStatusLabel === IN_PROGRESS_STATUS
    ) {
      const k = c[c.length - 1].segmentId.concat('-', targetLanguage);
      const n = {};
      if (c[c.length - 1].isPartial === false && cacheSeen[k] === undefined) {
        n[k] = { seen: true };
        setCacheSeen((state) => ({
          ...state,
          ...n,
        }));

        // prettier-ignore
        if (translateCache[k] === undefined) {
          // Now call translate API
          const params = {
            Text: c[c.length - 1].transcript,
            SourceLanguageCode: 'auto',
            TargetLanguageCode: targetLanguage,
          };
          const command = new TranslateTextCommand(params);

          logger.debug('Translate API being invoked for:', c[c.length - 1].transcript, targetLanguage);

          try {
            const data = await translateClient.send(command);
            const o = {};
            logger.debug('Translate API response:', c[c.length - 1].transcript, data.TranslatedText);
            o[k] = {
              cacheId: k,
              transcript: c[c.length - 1].transcript,
              translated: data.TranslatedText,
            };
            setTranslateCache((state) => ({
              ...state,
              ...o,
            }));
          } catch (error) {
            logger.debug('Error from translate:', error);
          }
        }
      }
      if (Date.now() - lastUpdated > 500) {
        setUpdateFlag((state) => !state);
        logger.debug('Updating turn by turn with latest cache');
      }
    }
    setLastUpdated(Date.now());
  }, [callTranscriptPerCallId]);

  const getTurnByTurnSegments = () => {
    const currentTurnByTurnSegments = transcriptChannels
      .map((c) => {
        const { segments } = transcriptsForThisCallId[c];
        return segments;
      })
      // sort entries by end time
      .reduce((p, c) => [...p, ...c].sort((a, b) => a.endTime - b.endTime), [])
      .map((c) => {
        const t = c;
        t.agentTranscript = agentTranscript;
        t.targetLanguage = targetLanguage;
        t.translateOn = translateOn;
        return t;
      })
      .map(
        // prettier-ignore
        (s) => (
          s?.segmentId
          && s?.createdAt
          && (s.agentTranscript === undefined
              || s.agentTranscript || s.channel !== 'AGENT')
          && (s.channel !== 'AGENT_VOICETONE')
          && (s.channel !== 'CALLER_VOICETONE')
          && <TranscriptSegment key={`${s.segmentId}-${s.createdAt}`} segment={s} translateCache={translateCache} />
        ),
      );

    // this element is used for scrolling to bottom and to provide padding
    currentTurnByTurnSegments.push(<div key="bottom" ref={bottomRef} />);
    return currentTurnByTurnSegments;
  };

  useEffect(() => {
    setTurnByTurnSegments(getTurnByTurnSegments);
  }, [
    callTranscriptPerCallId,
    item.recordingStatusLabel,
    targetLanguage,
    agentTranscript,
    translateOn,
    updateFlag,
  ]);

  useEffect(() => {
    // prettier-ignore
    if (
      item.recordingStatusLabel === IN_PROGRESS_STATUS
      && autoScroll
      && bottomRef.current?.scrollIntoView
    ) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [
    turnByTurnSegments,
    autoScroll,
    item.recordingStatusLabel,
    targetLanguage,
    agentTranscript,
    translateOn,
  ]);

  return (
    <div
      style={{
        overflowY: 'auto',
        maxHeight: collapseSentiment ? '34vh' : '68vh',
        paddingLeft: '10px',
        paddingTop: '5px',
        paddingRight: '10px',
      }}
    >
      <ColumnLayout borders="horizontal" columns={1}>
        {turnByTurnSegments}
      </ColumnLayout>
    </div>
  );
};

const getAgentAssistPanel = (item, collapseSentiment) => {
  if (process.env.REACT_APP_ENABLE_LEX_AGENT_ASSIST === 'true') {
    return (
      <Container
        disableContentPaddings
        header={
          <Header
            variant="h4"
            info={
              <Link variant="info" target="_blank" href="https://amazon.com/live-call-analytics">
                Info
              </Link>
            }
          >
            Agent Assist Bot
          </Header>
        }
      >
        <Box style={{ height: collapseSentiment ? '34vh' : '68vh' }}>
          <iframe
            style={{ border: '0px', height: collapseSentiment ? '34vh' : '68vh', margin: '0' }}
            title="Agent Assist"
            src={`/index-lexwebui.html?callId=${item.callId}`}
            width="100%"
          />
        </Box>
      </Container>
    );
  }
  return null;
};
const getTranscriptContent = ({
  item,
  callTranscriptPerCallId,
  autoScroll,
  translateClient,
  targetLanguage,
  agentTranscript,
  translateOn,
  collapseSentiment,
}) => {
  switch (item.recordingStatusLabel) {
    case DONE_STATUS:
    case IN_PROGRESS_STATUS:
    default:
      return (
        <CallInProgressTranscript
          item={item}
          callTranscriptPerCallId={callTranscriptPerCallId}
          autoScroll={autoScroll}
          translateClient={translateClient}
          targetLanguage={targetLanguage}
          agentTranscript={agentTranscript}
          translateOn={translateOn}
          collapseSentiment={collapseSentiment}
        />
      );
  }
};

const CallTranscriptContainer = ({
  setToolsOpen,
  item,
  callTranscriptPerCallId,
  translateClient,
  collapseSentiment,
}) => {
  // defaults to auto scroll when call is in progress
  const [autoScroll, setAutoScroll] = useState(item.recordingStatusLabel === IN_PROGRESS_STATUS);
  const [autoScrollDisabled, setAutoScrollDisabled] = useState(
    item.recordingStatusLabel !== IN_PROGRESS_STATUS,
  );

  const [translateOn, setTranslateOn] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState(
    localStorage.getItem('targetLanguage') || '',
  );
  const [agentTranscript, setAgentTranscript] = useState(true);

  const handleLanguageSelect = (event) => {
    setTargetLanguage(event.target.value);
    localStorage.setItem('targetLanguage', event.target.value);
  };

  useEffect(() => {
    setAutoScrollDisabled(item.recordingStatusLabel !== IN_PROGRESS_STATUS);
    setAutoScroll(item.recordingStatusLabel === IN_PROGRESS_STATUS);
  }, [item.recordingStatusLabel]);

  const languageChoices = () => {
    if (translateOn) {
      return (
        // prettier-ignore
        // eslint-disable-jsx-a11y/control-has-associated-label
        <div>
          <select value={targetLanguage} onChange={handleLanguageSelect}>
            {languageCodes.map(({ value, label }) => <option value={value}>{label}</option>)}
          </select>
        </div>
      );
    }
    return translateOn;
  };
  return (
    <Grid
      gridDefinition={[
        {
          colspan: {
            default: 12,
            xs: process.env.REACT_APP_ENABLE_LEX_AGENT_ASSIST === 'true' ? 8 : 12,
          },
        },
        {
          colspan: {
            default: 12,
            xs: process.env.REACT_APP_ENABLE_LEX_AGENT_ASSIST === 'true' ? 4 : 0,
          },
        },
      ]}
    >
      <Container
        fitHeight="true"
        disableContentPaddings
        header={
          <Header
            variant="h4"
            info={<InfoLink onFollow={() => setToolsOpen(true)} />}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Toggle
                  onChange={({ detail }) => setAutoScroll(detail.checked)}
                  checked={autoScroll}
                  disabled={autoScrollDisabled}
                />
                <span>Auto Scroll</span>
                <Toggle
                  onChange={({ detail }) => setAgentTranscript(detail.checked)}
                  checked={agentTranscript}
                />
                <span>Show Agent Transcripts?</span>
                <Toggle
                  onChange={({ detail }) => setTranslateOn(detail.checked)}
                  checked={translateOn}
                />
                <span>Enable Translation</span>
                {languageChoices()}
              </SpaceBetween>
            }
          >
            Call Transcript
          </Header>
        }
      >
        {getTranscriptContent({
          item,
          callTranscriptPerCallId,
          autoScroll,
          translateClient,
          targetLanguage,
          agentTranscript,
          translateOn,
          collapseSentiment,
        })}
      </Container>
      {getAgentAssistPanel(item, collapseSentiment)}
    </Grid>
  );
};

const VoiceToneContainer = ({
  item,
  callTranscriptPerCallId,
  collapseSentiment,
  setCollapseSentiment,
}) => (
  <Container
    fitHeight="true"
    disableContentPaddings={collapseSentiment ? '' : 'true'}
    header={
      <Header
        variant="h4"
        info={
          <Link
            variant="info"
            target="_blank"
            href="https://docs.aws.amazon.com/chime-sdk/latest/dg/call-analytics.html"
          >
            Info
          </Link>
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="inline-icon"
              iconName={collapseSentiment ? 'angle-up' : 'angle-down'}
              onClick={() => setCollapseSentiment(!collapseSentiment)}
            />
          </SpaceBetween>
        }
      >
        Voice Tone Analysis (30sec rolling window)
      </Header>
    }
  >
    {collapseSentiment ? (
      <VoiceToneFluctuationChart item={item} callTranscriptPerCallId={callTranscriptPerCallId} />
    ) : null}
  </Container>
);

const CallStatsContainer = ({
  item,
  callTranscriptPerCallId,
  collapseSentiment,
  setCollapseSentiment,
}) => (
  <>
    <Container
      disableContentPaddings={collapseSentiment ? '' : 'true'}
      header={
        <Header
          variant="h4"
          info={
            <Link
              variant="info"
              target="_blank"
              href="https://docs.aws.amazon.com/transcribe/latest/dg/call-analytics-insights.html#call-analytics-insights-sentiment"
            >
              Info
            </Link>
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="inline-icon"
                iconName={collapseSentiment ? 'angle-up' : 'angle-down'}
                onClick={() => setCollapseSentiment(!collapseSentiment)}
              />
            </SpaceBetween>
          }
        >
          Call Sentiment Analysis
        </Header>
      }
    >
      {collapseSentiment ? (
        <Grid gridDefinition={[{ colspan: 6 }, { colspan: 6 }]}>
          <SentimentFluctuationChart
            item={item}
            callTranscriptPerCallId={callTranscriptPerCallId}
          />
          <SentimentPerQuarterChart item={item} callTranscriptPerCallId={callTranscriptPerCallId} />
        </Grid>
      ) : null}
    </Container>
    {collapseSentiment ? (
      <Container style={{ display: collapseSentiment ? 'block' : 'none' }}>
        <ColumnLayout columns={4} variant="text-grid">
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Caller Avg Sentiment:</strong>
              </Box>
              <div>
                <SentimentIcon sentiment={item.callerSentimentLabel} />
                &nbsp;
                {item.callerAverageSentiment.toFixed(3)}
                <br />
                (min: -5, max: +5)
              </div>
            </div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Caller Sentiment Trend:</strong>
              </Box>
              <div>
                <SentimentTrendIcon trend={item.callerSentimentTrendLabel} />
              </div>
            </div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Agent Avg Sentiment:</strong>
              </Box>
              <div>
                <SentimentIcon sentiment={item.agentSentimentLabel} />
                &nbsp;
                {item.agentAverageSentiment.toFixed(3)}
                <br />
                (min: -5, max: +5)
              </div>
            </div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Agent Sentiment Trend:</strong>
              </Box>
              <div>
                <SentimentTrendIcon trend={item.agentSentimentTrendLabel} />
              </div>
            </div>
          </SpaceBetween>
        </ColumnLayout>
      </Container>
    ) : null}
  </>
);

export const CallPanel = ({ item, callTranscriptPerCallId, setToolsOpen }) => {
  const { currentCredentials } = useAppContext();

  const { settings } = useSettingsContext();
  const [collapseSentiment, setCollapseSentiment] = useState(false);

  const enableVoiceTone = settings?.EnableVoiceToneAnalysis === 'true';

  // prettier-ignore
  const customRetryStrategy = new StandardRetryStrategy(
    async () => MAXIMUM_ATTEMPTS,
    {
      delayDecider:
        (_, attempts) => Math.floor(
          Math.min(MAXIMUM_RETRY_DELAY, 2 ** attempts * 10),
        ),
    },
  );

  let translateClient = new TranslateClient({
    region: awsExports.aws_project_region,
    credentials: currentCredentials,
    maxAttempts: MAXIMUM_ATTEMPTS,
    retryStrategy: customRetryStrategy,
  });

  /* Get a client with refreshed credentials. Credentials can go stale when user is logged in
     for an extended period.
   */
  useEffect(() => {
    logger.debug('Translate client with refreshed credentials');
    translateClient = new TranslateClient({
      region: awsExports.aws_project_region,
      credentials: currentCredentials,
      maxAttempts: MAXIMUM_ATTEMPTS,
      retryStrategy: customRetryStrategy,
    });
  }, [currentCredentials]);

  return (
    <SpaceBetween size="s">
      <CallAttributes item={item} setToolsOpen={setToolsOpen} />
      <Grid
        gridDefinition={[{ colspan: { default: 12, xs: 8 } }, { colspan: { default: 12, xs: 4 } }]}
      >
        <CallSummary item={item} />
        <CallCategories item={item} />
      </Grid>
      <Grid
        gridDefinition={[
          { colspan: { default: 12, xs: enableVoiceTone ? 8 : 12 } },
          { colspan: { default: 12, xs: enableVoiceTone ? 4 : 0 } },
        ]}
      >
        <CallStatsContainer
          item={item}
          callTranscriptPerCallId={callTranscriptPerCallId}
          collapseSentiment={collapseSentiment}
          setCollapseSentiment={setCollapseSentiment}
        />
        {enableVoiceTone && (
          <VoiceToneContainer
            item={item}
            callTranscriptPerCallId={callTranscriptPerCallId}
            collapseSentiment={collapseSentiment}
            setCollapseSentiment={setCollapseSentiment}
          />
        )}
      </Grid>

      <CallTranscriptContainer
        item={item}
        setToolsOpen={setToolsOpen}
        callTranscriptPerCallId={callTranscriptPerCallId}
        translateClient={translateClient}
        collapseSentiment={collapseSentiment}
      />
    </SpaceBetween>
  );
};

export default CallPanel;
