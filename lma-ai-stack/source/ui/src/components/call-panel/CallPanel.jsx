/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  ColumnLayout,
  Container,
  FormField,
  Grid,
  Header,
  Icon,
  Input,
  Link,
  Modal,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Tabs,
  TextContent,
  Textarea,
  Toggle,
} from '@awsui/components-react';
import rehypeRaw from 'rehype-raw';
import ReactMarkdown from 'react-markdown';
import { TranslateClient, TranslateTextCommand } from '@aws-sdk/client-translate';
import { API, Logger, graphqlOperation } from 'aws-amplify';
import { StandardRetryStrategy } from '@aws-sdk/middleware-retry';
import { getEmailFormattedSummary, getMarkdownSummary, getTextFileFormattedMeetingDetails } from '../common/summary';
import { COMPREHEND_PII_TYPES, DEFAULT_OTHER_SPEAKER_NAME, LANGUAGE_CODES } from '../common/constants';

import RecordingPlayer from '../recording-player';
import useSettingsContext from '../../contexts/settings';

import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { InfoLink } from '../common/info-link';
import { getWeightedSentimentLabel } from '../common/sentiment';

import { VoiceToneFluctuationChart, SentimentFluctuationChart, SentimentPerQuarterChart } from './sentiment-charts';

import './CallPanel.css';
import { SentimentTrendIcon } from '../sentiment-trend-icon/SentimentTrendIcon';
import { SentimentIcon } from '../sentiment-icon/SentimentIcon';
import useAppContext from '../../contexts/app';
import awsExports from '../../aws-exports';
import {
  downloadTranscriptAsExcel,
  downloadTranscriptAsText,
  exportToTextFile,
  downloadTranscriptAsDocx,
  exportToDocxFile,
} from '../common/download-func';
import useCallsContext from '../../contexts/calls';
import { shareModal, deleteModal } from '../common/meeting-controls';
import VNCViewer from '../virtual-participant-layout/VNCViewer';
import { listVirtualParticipants, onUpdateVirtualParticipant } from '../../graphql/queries/virtualParticipantQueries';

const logger = new Logger('CallPanel');

// comprehend PII types
const piiTypesSplitRegEx = new RegExp(`\\[(${COMPREHEND_PII_TYPES.join('|')})\\]`);

const MAXIMUM_ATTEMPTS = 100;
const MAXIMUM_RETRY_DELAY = 1000;

const PAUSE_TO_MERGE_IN_SECONDS = 1;

/* eslint-disable react/prop-types, react/destructuring-assignment */
const CallAttributes = ({ item, setToolsOpen, getCallDetailsFromCallIds }) => {
  const { calls } = useCallsContext();
  const props = {
    calls,
    selectedItems: [item],
    loading: false,
    getCallDetailsFromCallIds,
  };

  return (
    <Container
      header={
        <Header
          variant="h4"
          info={<InfoLink onFollow={() => setToolsOpen(true)} />}
          actions={
            <SpaceBetween size="xxxs" direction="horizontal">
              {shareModal(props)} {deleteModal(props)}
            </SpaceBetween>
          }
        >
          Meeting Attributes
        </Header>
      }
    >
      <ColumnLayout columns={6} variant="text-grid">
        <SpaceBetween size="xs">
          <div>
            <Box margin={{ bottom: 'xxxs' }} color="text-label">
              <strong>Meeting ID</strong>
            </Box>
            <div>{item.callId}</div>
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
              <strong>Status</strong>
            </Box>
            <StatusIndicator type={item.recordingStatusIcon}>{` ${item.recordingStatusLabel} `}</StatusIndicator>
          </div>
        </SpaceBetween>
        {item?.pcaUrl?.length && (
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Post Meeting Analytics</strong>
              </Box>
              <Button variant="normal" href={item.pcaUrl} target="_blank" iconAlign="right" iconName="external">
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
};

// eslint-disable-next-line arrow-body-style
const CallSummary = ({ item }) => {
  const [setCopySuccess] = useState(false);

  const copyToClipboard = async () => {
    try {
      const summaryText = getTextFileFormattedMeetingDetails(item);
      await navigator.clipboard.writeText(summaryText);
      setCopySuccess(true);
      // Reset the success state after 2 seconds
      setTimeout(() => setCopySuccess(false), 2000);
    } catch (err) {
      logger.error('Failed to copy to clipboard:', err);
      // Fallback for older browsers
      try {
        const textArea = document.createElement('textarea');
        textArea.value = getTextFileFormattedMeetingDetails(item);
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        setCopySuccess(true);
        setTimeout(() => setCopySuccess(false), 2000);
      } catch (fallbackErr) {
        logger.error('Fallback copy failed:', fallbackErr);
      }
    }
  };

  const downloadCallSummary = async (option) => {
    if (option.detail.id === 'download') {
      await exportToTextFile(getTextFileFormattedMeetingDetails(item), `Summary-${item.callId}`);
    } else if (option.detail.id === 'email') {
      window.open(`mailto:?subject=${item.callId}&body=${getEmailFormattedSummary(item.callSummaryText)}`);
    } else if (option.detail.id === 'docx') {
      await exportToDocxFile(getTextFileFormattedMeetingDetails(item), `Summary-${item.callId}`);
    } else if (option.detail.id === 'copy') {
      await copyToClipboard();
    }
  };

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
          actions={
            <SpaceBetween size="xxs" direction="horizontal">
              {item.callSummaryText && (
                <ButtonDropdown
                  items={[
                    { text: 'Copy to clipboard', id: 'copy', disabled: false, iconName: 'copy' },
                    { text: 'Download summary', id: 'download', disabled: false, iconName: 'download' },
                    { text: 'Email summary (beta)', id: 'email', disabled: false, iconName: 'envelope' },
                    { text: 'Download as Word', id: 'docx', disabled: false, iconName: 'file' },
                  ]}
                  variant="normal"
                  onItemClick={(option) => downloadCallSummary(option)}
                >
                  <Icon name="download" variant="primary" />
                </ButtonDropdown>
              )}
            </SpaceBetween>
          }
        >
          Meeting Summary
        </Header>
      }
    >
      <Grid gridDefinition={[{ colspan: { default: 12 } }]}>
        <Tabs
          tabs={[
            {
              label: 'Transcript Summary',
              id: 'summary',
              content: (
                <div>
                  <div>
                    {/* eslint-disable-next-line react/no-array-index-key */}
                    <TextContent color="gray">
                      <ReactMarkdown rehypePlugins={[rehypeRaw]}>
                        {getMarkdownSummary(item.callSummaryText)}
                      </ReactMarkdown>
                    </TextContent>
                  </div>
                </div>
              ),
            },
          ]}
        />
      </Grid>
    </Container>
  );
};

const getSentimentImage = (segment, enableSentimentAnalysis) => {
  const { sentiment, sentimentScore, sentimentWeighted } = segment;
  if (!sentiment || !enableSentimentAnalysis) {
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

const TranscriptSegment = ({ segment, translateCache, enableSentimentAnalysis }) => {
  const { channel } = segment;

  if (channel === 'CATEGORY_MATCH') {
    const categoryText = `${segment.transcript}`;
    const newSegment = segment;
    newSegment.transcript = categoryText;
    // We will return a special version of the grid that's specifically only for category.
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
            {`${getTimestampFromSeconds(segment.startTime)} -
              ${getTimestampFromSeconds(segment.endTime)}`}
          </TextContent>
        </SpaceBetween>
        <TranscriptContent segment={segment} translateCache={translateCache} />
      </SpaceBetween>
    </Grid>
  );
};

/**
 * Check whether the current segment should be merged to the previous segment to get better
 * user experience. The conditions for merge are:
 * - Same speaker
 * - Same channel
 * - The gap between two segments is less than PAUSE_TO_MERGE_IN_SECONDS second
 * - Add language code check if available
 * TODO: Check language code once it is returned
 * @param previous previous segment
 * @param current current segment
 * @returns {boolean} indicates whether to merge or not
 */
const shouldAppendToPreviousSegment = ({ previous, current }) =>
  // prettier-ignore
  // eslint-disable-next-line implicit-arrow-linebreak
  previous.speaker === current.speaker
  && previous.channel === current.channel
  && current.startTime - previous.endTime < PAUSE_TO_MERGE_IN_SECONDS;

/**
 * Append current segment to its previous segment
 * @param previous previous segment
 * @param current current segment
 */
const appendToPreviousSegment = ({ previous, current }) => {
  /* eslint-disable no-param-reassign */
  previous.transcript += ` ${current.transcript}`;
  previous.endTime = current.endTime;
  previous.isPartial = current.isPartial;
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
  enableSentimentAnalysis,
}) => {
  const bottomRef = useRef();
  const containerRef = useRef();
  const [turnByTurnSegments, setTurnByTurnSegments] = useState([]);
  const [translateCache, setTranslateCache] = useState({});
  const [cacheSeen, setCacheSeen] = useState({});
  const [lastUpdated, setLastUpdated] = useState(Date.now());
  const [updateFlag, setUpdateFlag] = useState(false);
  const [userHasScrolled, setUserHasScrolled] = useState(false);

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
      .reduce((accumulator, current) => {
        if (
          // prettier-ignore
          !accumulator.length
          || !shouldAppendToPreviousSegment(
            { previous: accumulator[accumulator.length - 1], current },
          )
          // Enable it once it is compatible with translation
          || translateOn
        ) {
          // Get copy of current segment to avoid direct modification
          accumulator.push({ ...current });
        } else {
          appendToPreviousSegment({ previous: accumulator[accumulator.length - 1], current });
        }
        return accumulator;
      }, [])
      .map((c) => {
        const t = c;
        t.agentTranscript = agentTranscript;
        t.targetLanguage = targetLanguage;
        t.translateOn = translateOn;
        // In streaming audio the speaker will just be "Other participant", override this with the
        // name the user chose if needed
        if (t.speaker === DEFAULT_OTHER_SPEAKER_NAME || t.speaker === '') {
          t.speaker = item.callerPhoneNumber || DEFAULT_OTHER_SPEAKER_NAME;
        }

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
          && (s.channel !== 'CHAT_ASSISTANT')
          && <TranscriptSegment
            key={`${s.segmentId}-${s.createdAt}`}
            segment={s}
            translateCache={translateCache}
            enableSentimentAnalysis={enableSentimentAnalysis}
            participantName={item.callerPhoneNumber}
          />
        ),
      );

    // this element is used for scrolling to bottom and to provide padding
    currentTurnByTurnSegments.push(<div key="bottom" ref={bottomRef} />);
    return currentTurnByTurnSegments;
  };

  // Detect when user manually scrolls
  const handleScroll = (e) => {
    const container = e.target;
    const threshold = 50; // pixels from bottom
    const isAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
    if (!isAtBottom && autoScroll) {
      setUserHasScrolled(true);
    } else if (isAtBottom) {
      setUserHasScrolled(false);
    }
  };

  useEffect(() => {
    setTurnByTurnSegments(getTurnByTurnSegments);
  }, [callTranscriptPerCallId, item.recordingStatusLabel, targetLanguage, agentTranscript, translateOn, updateFlag]);

  useEffect(() => {
    // prettier-ignore
    if (
      item.recordingStatusLabel === IN_PROGRESS_STATUS
      && autoScroll
      && !userHasScrolled
      && bottomRef.current?.scrollIntoView
    ) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [
    turnByTurnSegments,
    autoScroll,
    userHasScrolled,
    item.recordingStatusLabel,
    targetLanguage,
    agentTranscript,
    translateOn,
  ]);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      style={{
        overflowY: 'auto',
        maxHeight: collapseSentiment ? '34vh' : '68vh',
        paddingLeft: '10px',
        paddingTop: '5px',
        paddingRight: '10px',
      }}
    >
      {/* Visual indicator when auto-scroll is paused */}
      {userHasScrolled && autoScroll && (
        <div
          style={{
            position: 'sticky',
            top: 0,
            background: '#ffeaa7',
            padding: '5px 10px',
            textAlign: 'center',
            fontSize: '12px',
            zIndex: 1000,
            borderRadius: '4px',
            margin: '0 0 10px 0',
            border: '1px solid #fdcb6e',
          }}
        >
          ⚠️ Auto-scroll paused - scroll to bottom to resume
          <Button
            onClick={() => {
              setUserHasScrolled(false);
              bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
            }}
            variant="inline-link"
            iconName="angle-down"
            style={{ marginLeft: '10px', fontSize: '12px' }}
          >
            Resume
          </Button>
        </div>
      )}
      <ColumnLayout borders="horizontal" columns={1}>
        {turnByTurnSegments}
      </ColumnLayout>
    </div>
  );
};

const getAgentAssistPanel = (item, collapseSentiment, user, showVNCPreview, setShowVNCPreview, vpData, loadingVP) => {
  const [showEditModal, setShowEditModal] = useState(false);
  const [buttonConfig, setButtonConfig] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [alertMessage, setAlertMessage] = useState(null);
  const [alertType, setAlertType] = useState('success');

  // Check if user is admin
  const userGroups = user?.signInUserSession?.accessToken?.payload['cognito:groups'] || [];
  const isAdmin = userGroups.includes('Admin');

  const loadButtonConfiguration = async () => {
    setIsLoading(true);
    setAlertMessage(null);

    try {
      const query = `
        query GetChatButtonConfig($defaultId: ID!, $customId: ID!) {
          default: getChatButtonConfig(ChatButtonConfigId: $defaultId) {
            ChatButtonConfigId
          }
          custom: getChatButtonConfig(ChatButtonConfigId: $customId) {
            ChatButtonConfigId
          }
        }
      `;

      const variables = {
        defaultId: 'DefaultChatButtonConfig',
        customId: 'CustomChatButtonConfig',
      };

      const result = await API.graphql(graphqlOperation(query, variables));

      // Parse the JSON strings returned from the resolver
      const defaultData = result?.data?.default?.ChatButtonConfigId
        ? JSON.parse(result.data.default.ChatButtonConfigId)
        : {};
      const customData = result?.data?.custom?.ChatButtonConfigId
        ? JSON.parse(result.data.custom.ChatButtonConfigId)
        : {};

      // Filter to only include button fields (format: N#LABEL)
      const buttonPattern = /^\d+#/;
      const filterButtons = (config) => {
        const filtered = {};
        Object.keys(config).forEach((key) => {
          if (buttonPattern.test(key)) {
            filtered[key] = config[key];
          }
        });
        return filtered;
      };

      const defaultButtons = filterButtons(defaultData);
      const customButtons = filterButtons(customData);

      // If custom buttons exist, use those; otherwise use defaults
      const hasCustomButtons = Object.keys(customButtons).length > 0;
      setButtonConfig(hasCustomButtons ? customButtons : defaultButtons);
    } catch (error) {
      console.error('Failed to load button configuration:', error);
      setAlertType('error');
      setAlertMessage('Failed to load button configuration');
    } finally {
      setIsLoading(false);
    }
  };

  // Load button configuration when modal opens
  useEffect(() => {
    if (showEditModal && isAdmin) {
      loadButtonConfiguration();
    }
  }, [showEditModal, isAdmin]);

  const handleSave = async () => {
    setIsLoading(true);
    setAlertMessage(null);

    try {
      const mutation = `
        mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
          updateChatButtonConfig(input: $input) {
            ChatButtonConfigId
            Success
          }
        }
      `;

      const variables = {
        input: {
          ChatButtonConfigId: 'CustomChatButtonConfig',
          ButtonConfig: JSON.stringify(buttonConfig),
        },
      };

      await API.graphql(graphqlOperation(mutation, variables));

      setAlertType('success');
      setAlertMessage('Button configuration saved successfully!');

      // Notify iframe to reload button configuration
      const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
      if (iframe && iframe.contentWindow) {
        iframe.contentWindow.postMessage(
          {
            type: 'STRANDS_RELOAD_BUTTONS',
          },
          '*',
        );
      }

      // Close modal after short delay
      setTimeout(() => {
        setShowEditModal(false);
      }, 1500);
    } catch (error) {
      console.error('Failed to save button configuration:', error);
      setAlertType('error');
      setAlertMessage('Failed to save button configuration');
    } finally {
      setIsLoading(false);
    }
  };

  const handleReset = async () => {
    if (
      !window.confirm('Are you sure you want to reset to default buttons? This will delete all custom configurations.')
    ) {
      return;
    }

    setIsLoading(true);
    setAlertMessage(null);

    try {
      const mutation = `
        mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
          updateChatButtonConfig(input: $input) {
            ChatButtonConfigId
            Success
          }
        }
      `;

      const variables = {
        input: {
          ChatButtonConfigId: 'CustomChatButtonConfig',
          ButtonConfig: JSON.stringify({}),
        },
      };

      await API.graphql(graphqlOperation(mutation, variables));

      setAlertType('success');
      setAlertMessage('Reset to defaults successfully!');

      // Notify iframe to reload button configuration
      const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
      if (iframe && iframe.contentWindow) {
        iframe.contentWindow.postMessage(
          {
            type: 'STRANDS_RELOAD_BUTTONS',
          },
          '*',
        );
      }

      // Reload to show default config
      setTimeout(() => {
        loadButtonConfiguration();
      }, 1500);
    } catch (error) {
      console.error('Failed to reset button configuration:', error);
      setAlertType('error');
      setAlertMessage('Failed to reset to defaults');
    } finally {
      setIsLoading(false);
    }
  };

  const handleButtonChange = (key, field, value) => {
    if (field === 'label') {
      // Update the key when label changes
      const match = key.match(/^(\d+)#/);
      if (match) {
        const sequence = parseInt(match[1], 10);
        const newKey = `${sequence}#${value}`;
        const newConfig = { ...buttonConfig };
        const oldPrompt = newConfig[key];
        delete newConfig[key];
        newConfig[newKey] = oldPrompt;
        setButtonConfig(newConfig);
      }
    } else {
      // Update prompt
      setButtonConfig({
        ...buttonConfig,
        [key]: value,
      });
    }
  };

  const renderButtonEditors = () => {
    if (!buttonConfig) return null;

    // Sort by sequence number
    const sortedEntries = Object.entries(buttonConfig).sort((a, b) => {
      const getSequence = (key) => {
        const match = key.match(/^(\d+)#/);
        return match ? parseInt(match[1], 10) : 999;
      };
      return getSequence(a[0]) - getSequence(b[0]);
    });

    return sortedEntries.map(([key, prompt]) => {
      const match = key.match(/^(\d+)#(.+)$/);
      if (!match) return null;

      const [, sequence, label] = match;

      return (
        <Box key={key} padding={{ vertical: 's' }}>
          <SpaceBetween size="s">
            <Box variant="h4">Button {sequence}</Box>
            <FormField label="Label">
              <Input value={label} onChange={(e) => handleButtonChange(key, 'label', e.detail.value)} />
            </FormField>
            <FormField label='Prompt (set to "NONE" to hide button)'>
              <Textarea value={prompt} onChange={(e) => handleButtonChange(key, 'prompt', e.detail.value)} rows={3} />
            </FormField>
          </SpaceBetween>
        </Box>
      );
    });
  };

  if (process.env.REACT_APP_ENABLE_AGENT_ASSIST === 'true') {
    // Use STRANDS UI for Lambda mode, Lex UI for Lex mode
    const iframeSrc =
      process.env.REACT_APP_AGENT_ASSIST_MODE === 'LAMBDA'
        ? `/strands-chat.html?callId=${item.callId}`
        : `/index-lexwebui.html?callId=${item.callId}`;

    console.log(`DEBUG: Agent Assist Mode: ${process.env.REACT_APP_AGENT_ASSIST_MODE}, Using iframe: ${iframeSrc}`);

    return (
      <>
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
              actions={
                isAdmin && process.env.REACT_APP_AGENT_ASSIST_MODE === 'LAMBDA' ? (
                  <ButtonDropdown
                    items={[
                      { text: 'Edit Chat Buttons', id: 'edit-buttons' },
                      {
                        text: showVNCPreview ? 'Close VP Live View' : 'Open VP Live View',
                        id: 'live-view',
                        disabled: !vpData || !vpData.vncReady || loadingVP,
                      },
                      { text: 'MCP Servers', id: 'mcp-servers', disabled: true },
                    ]}
                    variant="normal"
                    onItemClick={(e) => {
                      if (e.detail.id === 'edit-buttons') {
                        setShowEditModal(true);
                      } else if (e.detail.id === 'live-view') {
                        setShowVNCPreview((prev) => !prev);
                      }
                    }}
                    ariaLabel="Settings"
                  >
                    <Icon name="settings" />
                  </ButtonDropdown>
                ) : null
              }
            >
              Meeting Assist Bot
            </Header>
          }
        >
          <Box style={{ height: collapseSentiment ? '34vh' : '68vh' }}>
            <iframe
              style={{ border: '0px', height: collapseSentiment ? '34vh' : '68vh', margin: '0' }}
              title="Meeting Assist"
              src={iframeSrc}
              width="100%"
            />
          </Box>
        </Container>

        {/* Edit Buttons Modal */}
        <Modal
          visible={showEditModal}
          onDismiss={() => setShowEditModal(false)}
          size="large"
          header="Edit Chat Buttons"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="normal" onClick={handleReset} disabled={isLoading}>
                  Reset to Defaults
                </Button>
                <Button variant="link" onClick={() => setShowEditModal(false)}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleSave} disabled={isLoading}>
                  Save Changes
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Alert type={alertType} visible={!!alertMessage}>
              {alertMessage}
            </Alert>

            <Box>
              Customize the suggestion buttons that appear in the chat. Changes will apply to all users. Set prompt to
              &quot;NONE&quot; to hide a button.
            </Box>

            {isLoading ? (
              <Box textAlign="center">Loading...</Box>
            ) : (
              <SpaceBetween size="m">{renderButtonEditors()}</SpaceBetween>
            )}
          </SpaceBetween>
        </Modal>
      </>
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
  enableSentimentAnalysis,
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
          enableSentimentAnalysis={enableSentimentAnalysis}
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
  enableSentimentAnalysis,
  user,
  showVNCPreview,
  setShowVNCPreview,
  vpData,
  loadingVP,
}) => {
  // defaults to auto scroll when call is in progress
  const [autoScroll, setAutoScroll] = useState(item.recordingStatusLabel === IN_PROGRESS_STATUS);
  const [autoScrollDisabled, setAutoScrollDisabled] = useState(item.recordingStatusLabel !== IN_PROGRESS_STATUS);
  const [showDownloadTranscript, setShowDownloadTranscripts] = useState(item.recordingStatusLabel === DONE_STATUS);

  const [translateOn, setTranslateOn] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState(localStorage.getItem('targetLanguage') || '');
  const [agentTranscript] = useState(true);

  const handleLanguageSelect = (event) => {
    setTargetLanguage(event.target.value);
    localStorage.setItem('targetLanguage', event.target.value);
  };

  useEffect(() => {
    setAutoScrollDisabled(item.recordingStatusLabel !== IN_PROGRESS_STATUS);
    setAutoScroll(item.recordingStatusLabel === IN_PROGRESS_STATUS);
    setShowDownloadTranscripts(item.recordingStatusLabel === DONE_STATUS);
  }, [item.recordingStatusLabel]);

  const languageChoices = () => {
    if (translateOn) {
      return (
        // prettier-ignore
        // eslint-disable-jsx-a11y/control-has-associated-label
        <div>
          <select value={targetLanguage} onChange={handleLanguageSelect}>
            {LANGUAGE_CODES.map(({ value, label }) => <option value={value}>{label}</option>)}
          </select>
        </div>
      );
    }
    return translateOn;
  };

  const downloadTranscript = (option) => {
    console.log('option', option);
    if (option.detail.id === 'text') {
      downloadTranscriptAsText(callTranscriptPerCallId, item);
    } else if (option.detail.id === 'excel') {
      downloadTranscriptAsExcel(callTranscriptPerCallId, item);
    } else if (option.detail.id === 'docx') {
      downloadTranscriptAsDocx(callTranscriptPerCallId, item);
    }
  };

  return (
    <Grid
      gridDefinition={[
        {
          colspan: {
            default: 12,
            xs: process.env.REACT_APP_ENABLE_AGENT_ASSIST === 'true' ? 8 : 12,
          },
        },
        {
          colspan: {
            default: 12,
            xs: process.env.REACT_APP_ENABLE_AGENT_ASSIST === 'true' ? 4 : 0,
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
              <SpaceBetween direction="vertical" size="xs">
                <SpaceBetween direction="horizontal" size="xs">
                  <Toggle
                    onChange={({ detail }) => setAutoScroll(detail.checked)}
                    checked={autoScroll}
                    disabled={autoScrollDisabled}
                  />
                  <span>Auto Scroll</span>
                  <Toggle onChange={({ detail }) => setTranslateOn(detail.checked)} checked={translateOn} />
                  <span>Enable Translation</span>
                  {languageChoices()}
                  {showDownloadTranscript && (
                    <SpaceBetween direction="horizontal" size="xs">
                      <ButtonDropdown
                        items={[
                          {
                            text: 'Download as',
                            iconName: 'download',
                            items: [
                              { text: 'Excel', id: 'excel', disabled: false },
                              { text: 'Text', id: 'text' },
                              { text: 'Word', id: 'docx' },
                            ],
                          },
                        ]}
                        variant="normal"
                        onItemClick={(option) => downloadTranscript(option)}
                      >
                        <Icon name="download" variant="primary" />
                      </ButtonDropdown>
                    </SpaceBetween>
                  )}
                </SpaceBetween>
              </SpaceBetween>
            }
          >
            Meeting Transcript
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
          enableSentimentAnalysis,
        })}
      </Container>
      {getAgentAssistPanel(item, collapseSentiment, user, showVNCPreview, setShowVNCPreview, vpData, loadingVP)}
    </Grid>
  );
};

const VoiceToneContainer = ({ item, callTranscriptPerCallId, collapseSentiment, setCollapseSentiment }) => (
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

const CallStatsContainer = ({ item, callTranscriptPerCallId, collapseSentiment, setCollapseSentiment }) => (
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
          Meeting Sentiment Analysis
        </Header>
      }
    >
      {collapseSentiment ? (
        <Grid gridDefinition={[{ colspan: 6 }, { colspan: 6 }]}>
          <SentimentFluctuationChart item={item} callTranscriptPerCallId={callTranscriptPerCallId} />
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

export const CallPanel = ({ item, callTranscriptPerCallId, setToolsOpen, getCallDetailsFromCallIds }) => {
  const { currentCredentials, user } = useAppContext();

  const { settings } = useSettingsContext();
  const [collapseSentiment, setCollapseSentiment] = useState(false);

  const enableVoiceTone = settings?.EnableVoiceToneAnalysis === 'true';
  const enableSentimentAnalysis = settings?.IsSentimentAnalysisEnabled === 'true';

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

  // Send user context to iframe when it loads
  useEffect(() => {
    const sendUserContextToIframe = () => {
      const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
      if (iframe && iframe.contentWindow && user) {
        // Extract user groups from Cognito token
        const userGroups = user?.signInUserSession?.accessToken?.payload['cognito:groups'] || [];
        const isAdmin = userGroups.includes('Admin');

        iframe.contentWindow.postMessage(
          {
            type: 'STRANDS_USER_CONTEXT',
            userGroups,
            isAdmin,
            email: user?.attributes?.email || '',
          },
          '*',
        );
      }
    };

    // Send user context after a short delay to ensure iframe is loaded
    const timer = setTimeout(sendUserContextToIframe, 500);

    return () => clearTimeout(timer);
  }, [user, item.callId]);

  // Add message handler for STRANDS iframe requests
  useEffect(() => {
    const handleMessage = async (event) => {
      // Handle user context request from iframe
      if (event.data && event.data.type === 'STRANDS_REQUEST_USER_CONTEXT') {
        const userGroups = user?.signInUserSession?.accessToken?.payload['cognito:groups'] || [];
        const isAdmin = userGroups.includes('Admin');

        const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
        if (iframe && iframe.contentWindow) {
          iframe.contentWindow.postMessage(
            {
              type: 'STRANDS_USER_CONTEXT',
              userGroups,
              isAdmin,
              email: user?.attributes?.email || '',
            },
            '*',
          );
        }
      }

      // Handle button configuration requests
      else if (event.data && event.data.type === 'STRANDS_BUTTON_CONFIG_REQUEST') {
        try {
          const query = `
            query GetChatButtonConfig($defaultId: ID!, $customId: ID!) {
              default: getChatButtonConfig(ChatButtonConfigId: $defaultId) {
                ChatButtonConfigId
              }
              custom: getChatButtonConfig(ChatButtonConfigId: $customId) {
                ChatButtonConfigId
              }
            }
          `;

          const variables = {
            defaultId: 'DefaultChatButtonConfig',
            customId: 'CustomChatButtonConfig',
          };

          const result = await API.graphql(graphqlOperation(query, variables));

          // Parse the JSON strings returned from the resolver
          const defaultData = result?.data?.default?.ChatButtonConfigId
            ? JSON.parse(result.data.default.ChatButtonConfigId)
            : {};
          const customData = result?.data?.custom?.ChatButtonConfigId
            ? JSON.parse(result.data.custom.ChatButtonConfigId)
            : {};

          // Filter to only include button fields (format: N#LABEL)
          const buttonPattern = /^\d+#/;
          const filterButtons = (config) => {
            const filtered = {};
            Object.keys(config).forEach((key) => {
              if (buttonPattern.test(key)) {
                filtered[key] = config[key];
              }
            });
            return filtered;
          };

          const defaultButtons = filterButtons(defaultData);
          const customButtons = filterButtons(customData);

          // If custom buttons exist, use only those; otherwise use defaults
          const hasCustomButtons = Object.keys(customButtons).length > 0;
          const mergedConfig = hasCustomButtons ? { ...customButtons } : { ...defaultButtons };

          // Send success response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: true,
                config: mergedConfig,
              },
              '*',
            );
          }
        } catch (error) {
          console.error('[CallPanel] getChatButtonConfig call failed:', error);
          logger.error('getChatButtonConfig call failed', error);

          // Send error response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: false,
                error: error.message || error.errors?.[0]?.message || 'getChatButtonConfig call failed',
              },
              '*',
            );
          }
        }
      }

      // Handle update button configuration requests (admin only)
      else if (event.data && event.data.type === 'STRANDS_UPDATE_BUTTON_CONFIG_REQUEST') {
        try {
          const mutation = `
            mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
              updateChatButtonConfig(input: $input) {
                ChatButtonConfigId
                Success
              }
            }
          `;

          const variables = {
            input: {
              ChatButtonConfigId: 'CustomChatButtonConfig',
              ButtonConfig: event.data.buttonConfig,
            },
          };

          const result = await API.graphql(graphqlOperation(mutation, variables));

          // Send success response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_UPDATE_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: true,
                result,
              },
              '*',
            );
          }
        } catch (error) {
          logger.error('updateChatButtonConfig call failed', error);

          // Send error response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_UPDATE_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: false,
                error: error.message || error.errors?.[0]?.message || 'updateChatButtonConfig call failed',
              },
              '*',
            );
          }
        }
      }

      // Handle chat message requests
      else if (event.data && event.data.type === 'STRANDS_CHAT_REQUEST') {
        try {
          const mutation = `
            mutation SendChatMessage($input: SendChatMessageInput!) {
              sendChatMessage(input: $input) {
                MessageId
                Status
                CallId
                Response
              }
            }
          `;

          const variables = {
            input: {
              CallId: event.data.callId,
              Message: event.data.message,
            },
          };

          const result = await API.graphql(graphqlOperation(mutation, variables));

          // Send success response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_CHAT_RESPONSE',
                messageId: event.data.messageId,
                success: true,
                result,
              },
              '*',
            );
          }
        } catch (error) {
          logger.error('sendChatMessage call failed', error);

          // Send error response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_CHAT_RESPONSE',
                messageId: event.data.messageId,
                success: false,
                error: error.message || error.errors?.[0]?.message || 'sendChatMessage call failed',
              },
              '*',
            );
          }
        }
      }

      // Handle token stream subscription setup
      else if (event.data && event.data.type === 'STRANDS_SETUP_TOKEN_SUBSCRIPTION') {
        try {
          const subscription = `
            subscription OnAddChatToken($callId: ID!, $messageId: ID!) {
              onAddChatToken(CallId: $callId, MessageId: $messageId) {
                CallId
                MessageId
                Token
                IsComplete
                Sequence
                Timestamp
              }
            }
          `;

          // Set up token subscription
          API.graphql(
            graphqlOperation(subscription, {
              callId: event.data.callId,
              messageId: event.data.messageId,
            }),
          ).subscribe({
            next: ({ value }) => {
              const token = value?.data?.onAddChatToken;
              if (token) {
                // Send token to the chat iframe
                event.source.postMessage(
                  {
                    type: 'STRANDS_TOKEN_MESSAGE',
                    token,
                  },
                  '*',
                );
              }
            },
            error: (error) => {
              logger.error('Token subscription error', error);
            },
          });
        } catch (error) {
          logger.error('Failed to set up token subscription', error);
        }
      }
    };

    window.addEventListener('message', handleMessage);

    return () => {
      window.removeEventListener('message', handleMessage);
    };
  }, [user]);

  // VNC Preview state at CallPanel level
  const [showVNCPreview, setShowVNCPreview] = useState(false);
  const [vpData, setVpData] = useState(null);
  const [loadingVP, setLoadingVP] = useState(false);

  // Fetch Virtual Participant data for this call
  useEffect(() => {
    const fetchVPData = async () => {
      if (!item.callId) return;

      try {
        setLoadingVP(true);
        const result = await API.graphql(graphqlOperation(listVirtualParticipants));

        const vps = result.data.listVirtualParticipants || [];
        const matchingVP = vps.find((vp) => vp.CallId === item.callId);

        if (matchingVP && matchingVP.vncReady) {
          setVpData(matchingVP);
          logger.info('Found VP for call:', matchingVP);
        } else {
          setVpData(null);
        }
      } catch (error) {
        logger.error('Error fetching VP data:', error);
        setVpData(null);
      } finally {
        setLoadingVP(false);
      }
    };

    fetchVPData();
  }, [item.callId]);

  // Subscribe to VP updates
  useEffect(() => {
    if (!vpData?.id) return undefined;

    const subscription = API.graphql(graphqlOperation(onUpdateVirtualParticipant)).subscribe({
      next: ({ value }) => {
        const updated = value?.data?.onUpdateVirtualParticipant;
        if (updated && updated.id === vpData.id) {
          setVpData((prev) => ({
            ...prev,
            ...updated,
          }));
          logger.info('VP updated:', updated);
        }
      },
      error: (err) => logger.error('VP subscription error:', err),
    });

    return () => subscription.unsubscribe();
  }, [vpData?.id]);

  return (
    <SpaceBetween size="s">
      <CallAttributes item={item} setToolsOpen={setToolsOpen} getCallDetailsFromCallIds={getCallDetailsFromCallIds} />

      {/* Show VNC Preview in place of Summary during active meeting if VP is available */}
      {showVNCPreview && vpData && vpData.vncReady && item.recordingStatusLabel === IN_PROGRESS_STATUS ? (
        <VNCViewer
          vpId={vpData.id}
          vncEndpoint={vpData.vncEndpoint}
          websocketUrl={vpData.vncEndpoint}
          status={vpData.status}
          manualActionType={vpData.manualActionType}
          manualActionMessage={vpData.manualActionMessage}
          manualActionTimeoutSeconds={vpData.manualActionTimeoutSeconds}
          manualActionStartTime={vpData.manualActionStartTime}
          compact
          onOpenNewTab={() => window.open(`/virtual-participants/${vpData.id}`, '_blank')}
          showHeader
        />
      ) : (
        <CallSummary item={item} />
      )}

      {(enableSentimentAnalysis || enableVoiceTone) && (
        <Grid
          gridDefinition={[
            { colspan: { default: 12, xs: enableVoiceTone && enableSentimentAnalysis ? 8 : 12 } },
            { colspan: { default: 12, xs: enableVoiceTone && enableSentimentAnalysis ? 4 : 0 } },
          ]}
        >
          {enableSentimentAnalysis && (
            <CallStatsContainer
              item={item}
              callTranscriptPerCallId={callTranscriptPerCallId}
              collapseSentiment={collapseSentiment}
              setCollapseSentiment={setCollapseSentiment}
            />
          )}
          {enableVoiceTone && (
            <VoiceToneContainer
              item={item}
              callTranscriptPerCallId={callTranscriptPerCallId}
              collapseSentiment={collapseSentiment}
              setCollapseSentiment={setCollapseSentiment}
            />
          )}
        </Grid>
      )}
      <CallTranscriptContainer
        item={item}
        setToolsOpen={setToolsOpen}
        callTranscriptPerCallId={callTranscriptPerCallId}
        translateClient={translateClient}
        collapseSentiment={collapseSentiment}
        enableSentimentAnalysis={enableSentimentAnalysis}
        user={user}
        showVNCPreview={showVNCPreview}
        setShowVNCPreview={setShowVNCPreview}
        vpData={vpData}
        loadingVP={loadingVP}
      />
    </SpaceBetween>
  );
};

export default CallPanel;
