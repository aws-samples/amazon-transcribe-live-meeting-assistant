/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
} from '@cloudscape-design/components';
import ReactMarkdown from 'react-markdown';
import { TranslateClient, TranslateTextCommand } from '@aws-sdk/client-translate';
import { StandardRetryStrategy } from '@aws-sdk/middleware-retry';
import markdownRehypePlugins from '../common/markdown-plugins';
import { getEmailFormattedSummary, getMarkdownSummary, getTextFileFormattedMeetingDetails } from '../common/summary';
import {
  SOURCE_MANUAL,
  STATUS_IDLE,
  isHandEdited,
  isRegenerating,
  parseSummaryState,
  regenerationFailure,
} from '../common/summary-state';
import { COMPREHEND_PII_TYPES, LANGUAGE_CODES } from '../common/constants';
import {
  identityKeyFor,
  parseSpeakerIdentities,
  resolveSpeaker,
  serializeSpeakerIdentities,
} from '../common/speaker-identities';
import SpeakerIdentitiesPanel from './SpeakerIdentitiesPanel';
import AgendaPanel from './AgendaPanel';
import CallCostPanel from './CallCostPanel';

import RecordingPlayer from '../recording-player';
import VideoPlayer from '../video-player';
import useSettingsContext from '../../contexts/settings';

import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { InfoLink } from '../common/info-link';
import { getWeightedSentimentLabel } from '../common/sentiment';

import { VoiceToneFluctuationChart, SentimentFluctuationChart, SentimentPerQuarterChart } from './sentiment-charts';

import './CallPanel.css';
import { SentimentTrendIcon } from '../sentiment-trend-icon/SentimentTrendIcon';
import { SentimentIcon } from '../sentiment-icon/SentimentIcon';
import useAppContext from '../../contexts/app';
import useUserGroups from '../../hooks/use-user-groups';
import { AccessDenied } from '../common/AdminOnly';
import { isConflict } from '../common/config-conflict';
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
import MCPServersModal from '../mcp-servers';

const client = generateClient();
const logger = new ConsoleLogger('CallPanel');

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
        {item?.videoRecordingUrl?.length && item?.recordingStatusLabel !== IN_PROGRESS_STATUS && (
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Recording Video</strong>
              </Box>
              <VideoPlayer videoRecordingUrl={item.videoRecordingUrl} />
            </div>
          </SpaceBetween>
        )}
      </ColumnLayout>
    </Container>
  );
};

/**
 * How long the local "starting" state bridges the gap to the backend's own mark.
 *
 * Pressing Regenerate returns before the run finishes -- summarising calls a
 * model once per prompt template and outlives a GraphQL request -- so progress
 * after that point is read from the meeting's stored summary state, which
 * arrives through onUpdateCall. This covers only the second or two before that
 * mark lands, and is a safety net for the case where it never does.
 */
const REGENERATE_HANDOVER_MS = 30000;

const EDIT_AFTER_MEETING_REASON =
  'Available once the meeting has ended, so the end-of-meeting summary cannot replace your text.';

// Exported for its own tests: regenerating is an asynchronous run whose
// progress and failure are read back from the meeting rather than from the
// mutation's answer, and that handover is worth driving directly.
// eslint-disable-next-line arrow-body-style
export const CallSummary = ({ item, onSave, onRegenerate, readOnly }) => {
  const [setCopySuccess] = useState(false);

  const summaryState = useMemo(() => parseSummaryState(item.callSummaryState), [item.callSummaryState]);
  // Local, and deliberately not the only source of progress: a second browser
  // tab, or another user, learns that a run is going from the stored state.
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);

  const regenerating = starting || isRegenerating(summaryState);
  const failure = startError || regenerationFailure(summaryState);
  const handEdited = isHandEdited(summaryState);
  // Editing waits until the meeting has ended. A meeting still running gets an
  // end-of-meeting summary from the Kinesis pipeline when it does end, and that
  // write replaces the text without asking -- so an edit made during the
  // meeting would be discarded with none of the confirmation an edit is
  // promised, and the panel would go on showing the Edited badge over generated
  // text, because addCallSummaryText does not write CallSummaryState.
  //
  // This narrows that window rather than closing it. `ENDED` is set on the END
  // event, and the orchestrator then runs the speaker-naming pass and a model
  // call per template before its summary lands, which takes minutes. An edit
  // saved in between is still replaced, and the stale Edited label still
  // follows. Closing it properly means having the pipeline's write reset the
  // state, which is a change to that mutation's input and to how the browser
  // carries the field over -- out of scope here, and recorded in the docs as
  // the behaviour it is.
  //
  // Regenerate stays available during a meeting, which is one of the documented
  // reasons to press it: a meeting summarised while it was still going has
  // since continued. A run that is still going when the meeting ends is
  // discarded by the regenerator rather than written, because the pipeline's
  // summary covers more of the transcript than that run saw.
  const meetingInProgress = item.recordingStatusLabel === IN_PROGRESS_STATUS;

  // Hand over from the local flag to the stored state as soon as anything about
  // the summary changes -- which the backend's "running" mark is. Keyed on the
  // raw strings so a re-render with an identical summary does not reset it.
  const fingerprint = `${item.callSummaryText ?? ''}|${item.callSummaryState ?? ''}`;
  const fingerprintAtStart = useRef(null);
  useEffect(() => {
    if (!starting) {
      return undefined;
    }
    if (fingerprintAtStart.current !== null && fingerprint !== fingerprintAtStart.current) {
      setStarting(false);
      return undefined;
    }
    const timer = setTimeout(() => setStarting(false), REGENERATE_HANDOVER_MS);
    return () => clearTimeout(timer);
  }, [starting, fingerprint]);

  // A refusal belongs to the press that produced it and does not outlive the
  // summary it was refused for. The common refusal is "a run is already going",
  // raised against a run in another tab; it is hidden while that run is in
  // flight and would otherwise come back beside the result when the run landed
  // and the spinner stopped covering it.
  useEffect(() => {
    setStartError(null);
  }, [fingerprint]);

  const startRegeneration = async () => {
    setConfirmReplace(false);
    setStartError(null);
    fingerprintAtStart.current = fingerprint;
    setStarting(true);
    const result = await onRegenerate();
    if (!result?.Success) {
      // Said out loud rather than left as a stopped spinner: the commonest
      // refusals are "a run is already going" and "not permitted", and neither
      // is something the user can work out from the button going quiet.
      setStartError(result?.Message || 'could not regenerate the summary');
      setStarting(false);
    }
  };

  const handleRegenerateClick = () => {
    // A hand-written summary is somebody's own work, and regenerating discards
    // it, so that case asks first. A generated one has nothing to lose.
    if (handEdited) {
      setConfirmReplace(true);
      return;
    }
    startRegeneration();
  };

  const beginEditing = () => {
    // Seeded with the text as rendered, not the raw stored value: a summary
    // produced from several prompt templates is stored as a JSON object, and
    // asking somebody to edit JSON to fix a sentence would be absurd. Saving
    // therefore turns a structured summary into the plain markdown document it
    // was being read as, which is what a hand-written summary is.
    setDraft(getMarkdownSummary(item.callSummaryText));
    setStartError(null);
    setEditing(true);
  };

  const saveEdit = async () => {
    setSaving(true);
    try {
      const saved = await onSave(draft);
      if (saved) {
        setEditing(false);
      }
    } finally {
      setSaving(false);
    }
  };

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
      // Both halves are encoded: a meeting name is free text, and
      // getEmailFormattedSummary returns an encoded body.
      const subject = encodeURIComponent(item.callId ?? '');
      window.open(`mailto:?subject=${subject}&body=${getEmailFormattedSummary(item.callSummaryText)}`);
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
            <SpaceBetween size="xxs" direction="horizontal" alignItems="center">
              {regenerating && <StatusIndicator type="loading">Regenerating</StatusIndicator>}
              {!regenerating && failure && (
                // Announced, because the outcome of pressing Regenerate -- and
                // especially a refusal -- is the whole feedback for that action.
                // Through nativeAttributes: Cloudscape's Box accepts neither
                // `role` nor `ariaLive` as a prop.
                <Box nativeAttributes={{ role: 'status', 'aria-live': 'polite' }}>
                  <StatusIndicator type="error">{failure}</StatusIndicator>
                </Box>
              )}
              {/* Shown beside a failure, not instead of it: whose words the
                  stored summary is does not depend on how the last run went,
                  and a failed regeneration is exactly when it matters that the
                  text on screen is somebody's own. */}
              {!regenerating && handEdited && !editing && <Badge color="blue">Edited</Badge>}
              {editing ? (
                <SpaceBetween size="xxs" direction="horizontal">
                  <Button onClick={() => setEditing(false)} disabled={saving}>
                    Cancel
                  </Button>
                  {/* A blank summary would store as an empty string, which
                      leaves nothing to read and disables Edit, so the only way
                      back would be Regenerate. */}
                  <Button variant="primary" onClick={saveEdit} loading={saving} disabled={!draft.trim()}>
                    Save summary
                  </Button>
                </SpaceBetween>
              ) : (
                <SpaceBetween size="xxs" direction="horizontal">
                  {onRegenerate && (
                    <Button
                      iconName="refresh"
                      loading={regenerating}
                      disabled={readOnly || saving}
                      onClick={handleRegenerateClick}
                    >
                      Regenerate
                    </Button>
                  )}
                  {onSave && (
                    <Button
                      iconName="edit"
                      disabled={readOnly || regenerating || meetingInProgress || !item.callSummaryText}
                      disabledReason={meetingInProgress && !readOnly ? EDIT_AFTER_MEETING_REASON : undefined}
                      onClick={beginEditing}
                    >
                      Edit
                    </Button>
                  )}
                </SpaceBetween>
              )}
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
      {/* Mounted only while it is wanted: Cloudscape renders a Modal's footer
          even when it is not visible, so a permanently mounted one leaves a
          second "Regenerate" button in the page for a click to land on. */}
      {confirmReplace && (
        <Modal
          visible
          onDismiss={() => setConfirmReplace(false)}
          header="Replace the edited summary?"
          footer={
            <Box float="right">
              <SpaceBetween size="xs" direction="horizontal">
                <Button onClick={() => setConfirmReplace(false)}>Cancel</Button>
                <Button variant="primary" onClick={startRegeneration}>
                  Regenerate
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          This summary was edited by hand. Regenerating replaces it with a newly generated one, and the edited text
          cannot be recovered.
        </Modal>
      )}
      <Grid gridDefinition={[{ colspan: { default: 12 } }]}>
        <Tabs
          tabs={[
            {
              label: 'Transcript Summary',
              id: 'summary',
              content: editing ? (
                <FormField
                  label="Meeting summary"
                  description="Markdown. Saving replaces the summary everywhere it is read, the download included."
                  stretch
                >
                  <Textarea
                    value={draft}
                    onChange={({ detail }) => setDraft(detail.value)}
                    rows={16}
                    disabled={saving}
                    ariaLabel="Meeting summary text"
                  />
                </FormField>
              ) : (
                <div>
                  <div>
                    {/* eslint-disable-next-line react/no-array-index-key */}
                    <TextContent color="gray">
                      <ReactMarkdown rehypePlugins={markdownRehypePlugins}>
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
  // ISO string is YYYY-MM-DDTHH:MM:SS.sssZ. The previous code took substr(14, 7)
  // = "MM:SS.s", which silently DROPPED the hours field — so a segment at
  // 1h48m rendered as "48:00.0". Include the hours component once the offset
  // reaches an hour (e.g. "1:48:00") and keep the compact "MM:SS.s" form below.
  const iso = new Date(secs * 1000).toISOString();
  if (secs >= 3600) {
    const hours = Math.floor(secs / 3600);
    return `${hours}:${iso.substr(14, 5)}`; // H:MM:SS
  }
  return iso.substr(14, 7); // MM:SS.s
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
    const translatedText = result;

    switch (channel) {
      case 'AGENT_ASSISTANT':
      case 'MEETING_ASSISTANT':
        className = 'transcript-segment-agent-assist';
        break;
      case 'AGENT':
      case 'CALLER':
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
        <ReactMarkdown rehypePlugins={markdownRehypePlugins}>{text.trim()}</ReactMarkdown>
        <ReactMarkdown className="translated-text" rehypePlugins={markdownRehypePlugins}>
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

/**
 * Rename the voice behind one segment, from the segment itself.
 *
 * Correcting a name where you are reading it is the natural gesture; making
 * someone leave the transcript and find the Speakers panel is not. The wording
 * spells out how many segments move, because the instinct is to assume you are
 * editing the one line in front of you -- naming is per voice, and the
 * single-segment option is the escape hatch for the case where separation put
 * one turn on the wrong voice.
 */
const SpeakerRenamePopover = ({ segment, onRenameSpeaker, locked }) => {
  const [draft, setDraft] = useState('');
  const [onlyThisSegment, setOnlyThisSegment] = useState(false);
  const [saving, setSaving] = useState(false);

  // Clearing the draft is what signals the save landed: the field's placeholder
  // is the segment's current speaker, which re-renders to the new name. The
  // result is awaited so a rejected write does not read as a successful one.
  const save = async () => {
    if (!draft.trim()) {
      return;
    }
    setSaving(true);
    try {
      const ok = await onRenameSpeaker(segment, draft.trim(), onlyThisSegment);
      if (ok !== false) {
        setDraft('');
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Popover
      triggerType="custom"
      position="bottom"
      size="medium"
      dismissButton={false}
      content={
        <SpaceBetween direction="vertical" size="xs">
          {locked && (
            <Box variant="small" color="text-status-info">
              This name is locked. Unlock it in the Speakers panel to change it.
            </Box>
          )}
          <FormField
            label="Speaker name"
            description={
              onlyThisSegment
                ? 'Applies to this segment only.'
                : 'Applies to every segment from this voice, including earlier ones.'
            }
          >
            <Input
              value={draft}
              onChange={({ detail }) => setDraft(detail.value)}
              onKeyDown={({ detail }) => {
                if (detail.key === 'Enter') {
                  save();
                }
              }}
              placeholder={`${segment.speaker}`.trim()}
              disabled={saving || locked}
              autoFocus
            />
          </FormField>
          <Toggle checked={onlyThisSegment} onChange={({ detail }) => setOnlyThisSegment(detail.checked)}>
            Only this segment
          </Toggle>
          <Button variant="primary" disabled={!draft.trim() || saving || locked} loading={saving} onClick={save}>
            Save
          </Button>
        </SpaceBetween>
      }
    >
      <Button variant="inline-link">
        <strong
          className={segment.speakerProposed ? 'transcript-speaker-proposed' : undefined}
          title={
            segment.speakerProposed
              ? 'Suggested from the transcript. Confirm or reject it in the Speakers panel.'
              : undefined
          }
        >
          {segment.speaker}
        </strong>
      </Button>
    </Popover>
  );
};

const TranscriptSegment = ({ segment, translateCache, enableSentimentAnalysis, onRenameSpeaker, readOnly }) => {
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
          {onRenameSpeaker && !readOnly && (channel === 'AGENT' || channel === 'CALLER') ? (
            <SpeakerRenamePopover segment={segment} onRenameSpeaker={onRenameSpeaker} locked={segment.speakerLocked} />
          ) : (
            <TextContent>
              <strong
                className={segment.speakerProposed ? 'transcript-speaker-proposed' : undefined}
                title={
                  segment.speakerProposed
                    ? 'Suggested from the transcript. Confirm or reject it in the Speakers panel.'
                    : undefined
                }
              >
                {displayChannel}
              </strong>
            </TextContent>
          )}
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
  // Keep every id that went into this block. A merged block renders under the
  // first segment's id, so a "only this segment" rename recorded against that id
  // alone would cover only part of the text the user was looking at -- visibly so
  // in the transcript download, which does not merge.
  previous.mergedSegmentIds = [...(previous.mergedSegmentIds || [previous.segmentId]), current.segmentId];
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
  speakerIdentities,
  onRenameSpeaker,
  readOnly,
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
  useEffect(() => {
    const runTranslation = async () => {
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
    };
    runTranslation();
  }, [callTranscriptPerCallId, translateOn, targetLanguage]);

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
        // Resolve the stored speaker to a display name. This covers both the
        // meeting's speaker identity map (a voice a user has named) and the older
        // behaviour of substituting the participant name typed on the Stream Audio
        // form for the 'Other Participant' placeholder. Resolution happens on
        // read, which is what makes naming a voice apply to segments that were
        // written before the name was known.
        // Capture the stored speaker before overwriting it: it is what the
        // identity map is keyed on, so the popover on this segment needs it to
        // rename the voice.
        // The key a name is stored under, resolved THROUGH any merge -- writing
        // to the segment's own key once it has been merged would store an entry
        // nothing reads.
        t.speakerKey = identityKeyFor(t, speakerIdentities);
        // callerPhoneNumber is the remote side's name, so it substitutes for the
        // placeholder on the CALLER channel only. The download path has always
        // scoped it this way; the other readers now match.
        const resolved = resolveSpeaker(
          t,
          speakerIdentities,
          t.channel === 'CALLER' ? item.callerPhoneNumber : undefined,
        );
        t.speaker = resolved.name;
        t.speakerLocked = resolved.locked;
        // An inferred name that nobody has confirmed. Rendered differently
        // below, because showing a guess as indistinguishable from a name a
        // person typed is the one thing that would make this actively harmful.
        t.speakerProposed = resolved.status === 'proposed';

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
            onRenameSpeaker={onRenameSpeaker}
            readOnly={readOnly}
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

  const scrollTranscriptToBottom = useCallback(() => {
    if (containerRef.current) {
      containerRef.current.scrollTo({
        top: containerRef.current.scrollHeight,
        behavior: 'smooth',
      });
    }
  }, []);

  useEffect(() => {
    setTurnByTurnSegments(getTurnByTurnSegments);
    // speakerIdentities belongs here: naming a voice has to re-label the
    // segments already on screen, which is the whole point of resolving the
    // speaker on read. Without it a rename would only affect later segments.
  }, [
    callTranscriptPerCallId,
    item.recordingStatusLabel,
    targetLanguage,
    agentTranscript,
    translateOn,
    updateFlag,
    speakerIdentities,
    // The elements carry the inline-rename affordance, so they have to be
    // rebuilt when it changes. isAdminUser resolves asynchronously, so readOnly
    // flips from true to false shortly after first render; without it here an
    // admin viewing someone else's ended meeting would keep elements built
    // while the answer was still "no".
    readOnly,
    onRenameSpeaker,
  ]);

  useEffect(() => {
    // prettier-ignore
    if (
      item.recordingStatusLabel === IN_PROGRESS_STATUS
      && autoScroll
      && !userHasScrolled
      && containerRef.current
    ) {
      scrollTranscriptToBottom();
    }
  }, [
    turnByTurnSegments,
    autoScroll,
    userHasScrolled,
    item.recordingStatusLabel,
    targetLanguage,
    agentTranscript,
    translateOn,
    scrollTranscriptToBottom,
  ]);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      style={{
        overflowY: 'auto',
        height: collapseSentiment ? '34vh' : '68vh',
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
              scrollTranscriptToBottom();
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

// Shown when the stored profile moved on while the Edit Chat Buttons dialog was
// open -- another administrator saved it, from this dialog or a configuration
// page, since they share one profile. The save is refused rather than replacing
// their edit, so the way forward is to reopen the dialog and reapply.
const CHAT_BUTTON_CONFLICT_MESSAGE =
  'Someone else saved the configuration while this dialog was open. Close and reopen it to see ' +
  'their version, then apply your changes again.';

const getAgentAssistPanel = (item, collapseSentiment, user, showVNCPreview, setShowVNCPreview, vpData, loadingVP) => {
  const [showEditModal, setShowEditModal] = useState(false);
  const [showMCPModal, setShowMCPModal] = useState(false);
  const [buttonConfig, setButtonConfig] = useState(null);
  // The version of the stored profile the dialog loaded, sent back on save so a
  // save against a profile someone else has since saved is refused rather than
  // replacing their edit.
  const [buttonConfigVersion, setButtonConfigVersion] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [alertMessage, setAlertMessage] = useState(null);
  const [alertType, setAlertType] = useState('success');

  // Check if user is admin (Amplify v6 pattern). `isAdminLoading` distinguishes
  // "not yet known" from "not an admin": the session resolves asynchronously, so
  // acting on isAdmin before then would show an admin the denial on first render.
  const { isAdmin, isLoading: isAdminLoading } = useUserGroups();

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

      const result = await client.graphql({ query, variables });

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
      setButtonConfigVersion(typeof customData.Version === 'number' ? customData.Version : null);
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
            Version
          }
        }
      `;

      const variables = {
        input: {
          ChatButtonConfigId: 'CustomChatButtonConfig',
          ButtonConfig: JSON.stringify(buttonConfig),
          ExpectedVersion: buttonConfigVersion,
        },
      };

      const saved = await client.graphql({ query: mutation, variables });
      // So a second save from the same dialog is checked against this one.
      setButtonConfigVersion(saved?.data?.updateChatButtonConfig?.Version ?? null);

      setAlertType('success');
      setAlertMessage('Button configuration saved successfully!');

      // Notify iframe to reload button configuration
      const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
      if (iframe && iframe.contentWindow) {
        const iframeOrigin = new URL(iframe.src).origin;
        iframe.contentWindow.postMessage(
          {
            type: 'STRANDS_RELOAD_BUTTONS',
          },
          iframeOrigin,
        );
      }

      // Close modal after short delay
      setTimeout(() => {
        setShowEditModal(false);
      }, 1500);
    } catch (error) {
      console.error('Failed to save button configuration:', error);
      setAlertType('error');
      setAlertMessage(isConflict(error) ? CHAT_BUTTON_CONFLICT_MESSAGE : 'Failed to save button configuration');
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
            Version
          }
        }
      `;

      const variables = {
        input: {
          ChatButtonConfigId: 'CustomChatButtonConfig',
          ButtonConfig: JSON.stringify({}),
          ExpectedVersion: buttonConfigVersion,
        },
      };

      const saved = await client.graphql({ query: mutation, variables });
      setButtonConfigVersion(saved?.data?.updateChatButtonConfig?.Version ?? null);

      setAlertType('success');
      setAlertMessage('Reset to defaults successfully!');

      // Notify iframe to reload button configuration
      const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
      if (iframe && iframe.contentWindow) {
        const iframeOrigin = new URL(iframe.src).origin;
        iframe.contentWindow.postMessage(
          {
            type: 'STRANDS_RELOAD_BUTTONS',
          },
          iframeOrigin,
        );
      }

      // Reload to show default config
      setTimeout(() => {
        loadButtonConfiguration();
      }, 1500);
    } catch (error) {
      console.error('Failed to reset button configuration:', error);
      setAlertType('error');
      setAlertMessage(isConflict(error) ? CHAT_BUTTON_CONFLICT_MESSAGE : 'Failed to reset to defaults');
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

  const handleDeleteButton = (key) => {
    const newConfig = { ...buttonConfig };
    delete newConfig[key];

    // Re-sequence remaining buttons to keep numbering contiguous
    const sortedEntries = Object.entries(newConfig).sort((a, b) => {
      const getSeq = (k) => {
        const m = k.match(/^(\d+)#/);
        return m ? parseInt(m[1], 10) : 999;
      };
      return getSeq(a[0]) - getSeq(b[0]);
    });

    const resequenced = {};
    sortedEntries.forEach(([k, v], idx) => {
      const label = k.replace(/^\d+#/, '');
      resequenced[`${idx + 1}#${label}`] = v;
    });

    setButtonConfig(resequenced);
  };

  const handleAddButton = () => {
    // Find the next sequence number
    const existingSequences = Object.keys(buttonConfig || {}).map((k) => {
      const m = k.match(/^(\d+)#/);
      return m ? parseInt(m[1], 10) : 0;
    });
    const nextSequence = existingSequences.length > 0 ? Math.max(...existingSequences) + 1 : 1;
    const newKey = `${nextSequence}#NEW BUTTON`;
    setButtonConfig({
      ...buttonConfig,
      [newKey]: 'Enter prompt here',
    });
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
      const match = key.match(/^(\d+)#(.*)$/);
      if (!match) return null;

      const [, sequence, label] = match;

      return (
        <Box key={`button-${sequence}`} padding={{ vertical: 's' }}>
          <SpaceBetween size="s">
            <SpaceBetween direction="horizontal" size="xs">
              <Box variant="h4">Button {sequence}</Box>
              <Button
                variant="icon"
                iconName="remove"
                onClick={() => handleDeleteButton(key)}
                ariaLabel={`Delete button ${sequence}`}
              />
            </SpaceBetween>
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

  if (import.meta.env.VITE_ENABLE_AGENT_ASSIST === 'true') {
    // Use STRANDS chat UI
    // Note: encodeURIComponent is required because callId may contain characters like '+'
    // which URLSearchParams.get() would otherwise decode as a space.
    const iframeSrc = `/strands-chat.html?callId=${encodeURIComponent(item.callId)}`;

    console.log(`DEBUG: Agent Assist Mode: ${import.meta.env.VITE_AGENT_ASSIST_MODE}, Using iframe: ${iframeSrc}`);

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
                isAdmin && import.meta.env.VITE_AGENT_ASSIST_MODE === 'LAMBDA' ? (
                  <ButtonDropdown
                    items={[
                      { text: 'Edit Chat Buttons', id: 'edit-buttons' },
                      {
                        text: showVNCPreview ? 'Close VP Live View' : 'Open VP Live View',
                        id: 'live-view',
                        disabled: !vpData || !vpData.vncReady || loadingVP,
                      },
                      { text: 'MCP Servers', id: 'mcp-servers', disabled: false },
                    ]}
                    variant="normal"
                    onItemClick={(e) => {
                      if (e.detail.id === 'edit-buttons') {
                        setShowEditModal(true);
                      } else if (e.detail.id === 'live-view') {
                        setShowVNCPreview((prev) => !prev);
                      } else if (e.detail.id === 'mcp-servers') {
                        setShowMCPModal(true);
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
            {/*
              The `sandbox` list is deliberately exactly these two tokens.

              What it buys: the frame renders agent output. The omitted tokens
              mean nothing in that output can submit a form, open a popup, raise
              a modal, or navigate the top-level page. Those are capabilities of
              the inert markup that reaches the DOM, not of script, so removing
              them is worth having on its own. This is the outer of three layers
              for the form case in particular -- renderMarkdownSafe in
              strands-chat.html strips form controls before they are rendered,
              and `form-action 'self'` in the distribution's CSP bounds where a
              submission could go -- and it is the layer that does not depend on
              the sanitizer having run.

              What it does not buy: `allow-scripts` plus `allow-same-origin` lets
              a script already running in the frame remove the sandbox attribute
              from its own frame element, so this is not a boundary against code
              executing inside the frame. It is a restriction on inert content.

              Why `allow-same-origin` stays: the parent/frame postMessage channel
              is pinned in both directions -- the parent only acts on messages
              whose `event.origin` equals its own, and addresses the frame with
              an explicit targetOrigin rather than '*'. Dropping the token would
              move the frame to an opaque origin and force both ends back to
              '*'/"null", trading a precise channel for a vaguer one.

              Cost: none. strands-chat.html has no form, no target="_blank", no
              window.open and no downloads of its own.
            */}
            <iframe
              style={{ border: '0px', height: collapseSentiment ? '34vh' : '68vh', margin: '0' }}
              title="Meeting Assist"
              src={iframeSrc}
              sandbox="allow-scripts allow-same-origin"
              width="100%"
            />
          </Box>
        </Container>

        {/*
          Edit Buttons Modal.

          The chat buttons are shared by everyone in the account, so saving them
          is an Admin operation, enforced on the AppSync field and in the
          resolver behind it.

          The !isAdmin branches below are a backstop, not a path a user takes
          today: the only thing that opens this dialog is the actions dropdown
          above, which is itself rendered only for an Admin, so a non-admin
          cannot reach it. They are here because this panel is built in one
          function and reused, and an entry point added later without that gate
          is exactly the mistake worth surviving -- the dialog then explains
          itself instead of offering a form whose save the resolver refuses.
          Nothing user-facing should be documented as reachable through it.
        */}
        <Modal
          visible={showEditModal}
          onDismiss={() => setShowEditModal(false)}
          size="large"
          header="Edit Chat Buttons"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                {isAdmin && (
                  <Button variant="normal" onClick={handleReset} disabled={isLoading}>
                    Reset to Defaults
                  </Button>
                )}
                <Button variant="link" onClick={() => setShowEditModal(false)}>
                  {isAdmin ? 'Cancel' : 'Close'}
                </Button>
                {isAdmin && (
                  <Button variant="primary" onClick={handleSave} disabled={isLoading}>
                    Save Changes
                  </Button>
                )}
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Alert type={alertType} visible={!!alertMessage}>
              {alertMessage}
            </Alert>

            {!isAdminLoading && !isAdmin ? (
              <AccessDenied feature="the chat buttons" />
            ) : (
              <>
                <Box>
                  Customize the suggestion buttons that appear in the chat. Changes will apply to all users. Set prompt
                  to &quot;NONE&quot; to hide a button.
                </Box>

                {isLoading || isAdminLoading ? (
                  <Box textAlign="center">Loading...</Box>
                ) : (
                  <SpaceBetween size="m">
                    {renderButtonEditors()}
                    <Button variant="normal" iconName="add-plus" onClick={handleAddButton}>
                      Add Button
                    </Button>
                  </SpaceBetween>
                )}
              </>
            )}
          </SpaceBetween>
        </Modal>

        {/* MCP Servers Modal */}
        <MCPServersModal visible={showMCPModal} onDismiss={() => setShowMCPModal(false)} vpData={vpData} />
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
  speakerIdentities,
  onRenameSpeaker,
  readOnly,
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
          speakerIdentities={speakerIdentities}
          onRenameSpeaker={onRenameSpeaker}
          readOnly={readOnly}
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
  speakerIdentities,
  onRenameSpeaker,
  readOnly,
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
            xs: import.meta.env.VITE_ENABLE_AGENT_ASSIST === 'true' ? 8 : 12,
          },
        },
        {
          colspan: {
            default: 12,
            xs: import.meta.env.VITE_ENABLE_AGENT_ASSIST === 'true' ? 4 : 0,
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
          speakerIdentities,
          onRenameSpeaker,
          readOnly,
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
  const { userGroups, isAdmin: isAdminUser } = useUserGroups();

  const { settings } = useSettingsContext();
  const [collapseSentiment, setCollapseSentiment] = useState(false);

  const enableVoiceTone = settings?.EnableVoiceToneAnalysis === 'true';
  const enableSentimentAnalysis = settings?.IsSentimentAnalysisEnabled === 'true';

  const {
    sendUpdateSpeakerIdentities,
    sendResolveSpeakerNames,
    sendUpdateCallSummary,
    sendRegenerateCallSummary,
    sendUpdateCallAgenda,
    sendDeriveCallAgenda,
  } = useCallsContext();
  // Memoised on the raw string: created in the component body it would be a new
  // object identity every render, and it is a dependency of the effect that
  // rebuilds the entire transcript element list.
  const speakerIdentities = useMemo(() => parseSpeakerIdentities(item.speakerIdentities), [item.speakerIdentities]);

  const saveSpeakerIdentities = (serialized) => sendUpdateSpeakerIdentities(item.callId, serialized);

  /**
   * Store a summary somebody wrote by hand.
   *
   * Recorded as source 'manual', which is what puts the Edited badge on the
   * panel and what makes a later regeneration ask before replacing it.
   */
  const saveCallSummary = (text) =>
    sendUpdateCallSummary(item.callId, text, {
      source: SOURCE_MANUAL,
      status: STATUS_IDLE,
      updatedAt: new Date().toISOString(),
    });

  const regenerateCallSummaryForItem = useCallback(
    () => sendRegenerateCallSummary(item.callId),
    [item.callId, sendRegenerateCallSummary],
  );

  // Presentation only -- it greys out controls the write would reject anyway.
  // The rule itself is enforced as a condition on the mutation (and, for the
  // regeneration, in the Lambda behind it). Shared by the Speakers panel, the
  // transcript's inline rename, the summary controls and the Agenda panel so
  // all four agree.
  const speakerNamesReadOnly =
    !isAdminUser && item.owner !== user?.username && !item.sharedWith?.includes(user?.username);

  /**
   * Apply a rename made from a transcript segment.
   *
   * Two different writes behind one gesture: by default the name is attached to
   * the VOICE, which relabels every segment that voice produced; with "only this
   * segment" it is recorded against the segment id instead, for the case where
   * speaker separation attributed a single turn to the wrong voice and no
   * voice-level rename can fix it.
   */
  // Memoised because it is a dependency of the effect that rebuilds the entire
  // transcript element list. A fresh identity each render would rebuild it on
  // every unrelated re-render of this page -- the calls context is reconstructed
  // on each render of the layout above, so those are frequent.
  const handleRenameSpeaker = useCallback(
    (segment, name, onlyThisSegment) => {
      if (onlyThisSegment) {
        const overrides = { ...speakerIdentities.segmentOverrides };
        (segment.mergedSegmentIds || [segment.segmentId]).forEach((id) => {
          overrides[id] = name;
        });
        return saveSpeakerIdentities(serializeSpeakerIdentities({ ...speakerIdentities, segmentOverrides: overrides }));
      }
      return saveSpeakerIdentities(
        serializeSpeakerIdentities({
          ...speakerIdentities,
          identities: {
            ...speakerIdentities.identities,
            [segment.speakerKey]: {
              ...(speakerIdentities.identities[segment.speakerKey] || {}),
              name,
              status: 'confirmed',
              source: 'manual',
              updatedAt: new Date().toISOString(),
            },
          },
        }),
      );
    },
    [speakerIdentities, item.callId, sendUpdateSpeakerIdentities],
  );

  const identifySpeakers = useCallback(
    ({ force }) => sendResolveSpeakerNames(item.callId, { force }),
    [item.callId, sendResolveSpeakerNames],
  );

  // Flattened final segments, for the Speakers panel's one-row-per-voice list.
  const transcriptSegmentsForCall = Object.values(callTranscriptPerCallId[item.callId] || {}).flatMap(
    (channel) => channel.segments || [],
  );

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
        const iframeOrigin = new URL(iframe.src).origin;
        iframe.contentWindow.postMessage(
          {
            type: 'STRANDS_USER_CONTEXT',
            userGroups,
            isAdmin: isAdminUser,
            email: user?.attributes?.email || user?.username || '',
          },
          iframeOrigin,
        );
      }
    };

    // Send user context after a short delay to ensure iframe is loaded
    const timer = setTimeout(sendUserContextToIframe, 500);

    return () => clearTimeout(timer);
  }, [user, userGroups, isAdminUser, item.callId]);

  // Add message handler for STRANDS iframe requests
  useEffect(() => {
    const handleMessage = async (event) => {
      // Validate message origin for security
      if (event.origin !== window.location.origin) return;

      // Handle user context request from iframe
      if (event.data && event.data.type === 'STRANDS_REQUEST_USER_CONTEXT') {
        const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
        if (iframe && iframe.contentWindow) {
          const iframeOrigin = new URL(iframe.src).origin;
          iframe.contentWindow.postMessage(
            {
              type: 'STRANDS_USER_CONTEXT',
              userGroups,
              isAdmin: isAdminUser,
              email: user?.attributes?.email || user?.username || '',
            },
            iframeOrigin,
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

          const result = await client.graphql({ query, variables });

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
            const iframeOrigin = new URL(iframe.src).origin;
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: true,
                config: mergedConfig,
              },
              iframeOrigin,
            );
          }
        } catch (error) {
          console.error('[CallPanel] getChatButtonConfig call failed:', error);
          logger.error('getChatButtonConfig call failed', error);

          // Send error response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            const iframeOrigin = new URL(iframe.src).origin;
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: false,
                error: error.message || error.errors?.[0]?.message || 'getChatButtonConfig call failed',
              },
              iframeOrigin,
            );
          }
        }
      }

      // Handle update button configuration requests (admin only).
      //
      // Nothing in LMA sends this message: strands-chat.html only reads its
      // buttons (STRANDS_BUTTON_CONFIG_REQUEST), and they are edited from the Edit
      // Chat Buttons dialog, which sends the version it loaded. This path sends
      // none, so its save is conditional on the record as the resolver reads it
      // at save time but not on what the sender was looking at; a sender that
      // wants that check has to supply the version itself.
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

          const result = await client.graphql({ query: mutation, variables });

          // Send success response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            const iframeOrigin = new URL(iframe.src).origin;
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_UPDATE_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: true,
                result,
              },
              iframeOrigin,
            );
          }
        } catch (error) {
          logger.error('updateChatButtonConfig call failed', error);

          // Send error response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            const iframeOrigin = new URL(iframe.src).origin;
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_UPDATE_BUTTON_CONFIG_RESPONSE',
                requestId: event.data.requestId,
                success: false,
                error: error.message || error.errors?.[0]?.message || 'updateChatButtonConfig call failed',
              },
              iframeOrigin,
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
              ConversationHistory: event.data.conversationHistory || [],
            },
          };

          const result = await client.graphql({ query: mutation, variables });

          // Send success response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            const iframeOrigin = new URL(iframe.src).origin;
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_CHAT_RESPONSE',
                messageId: event.data.messageId,
                success: true,
                result,
              },
              iframeOrigin,
            );
          }
        } catch (error) {
          logger.error('sendChatMessage call failed', error);

          // Send error response back to iframe
          const iframe = document.querySelector(`iframe[src*="strands-chat.html"]`);
          if (iframe && iframe.contentWindow) {
            const iframeOrigin = new URL(iframe.src).origin;
            iframe.contentWindow.postMessage(
              {
                type: 'STRANDS_CHAT_RESPONSE',
                messageId: event.data.messageId,
                success: false,
                error: error.message || error.errors?.[0]?.message || 'sendChatMessage call failed',
              },
              iframeOrigin,
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
                ThinkingStep {
                  Type
                  Content
                  ToolName
                  ToolInput
                  ToolResult
                  Success
                }
              }
            }
          `;

          // Set up token subscription
          client
            .graphql({
              query: subscription,
              variables: {
                callId: event.data.callId,
                messageId: event.data.messageId,
              },
            })
            .subscribe({
              next: (message) => {
                const token = message?.data?.onAddChatToken;
                if (token) {
                  // Send token to the chat iframe
                  event.source.postMessage(
                    {
                      type: 'STRANDS_TOKEN_MESSAGE',
                      token,
                    },
                    event.origin,
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
        const result = await client.graphql({ query: listVirtualParticipants });

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

    const subscription = client.graphql({ query: onUpdateVirtualParticipant }).subscribe({
      next: (message) => {
        const updated = message?.data?.onUpdateVirtualParticipant;
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

  // Subscribe to VNC preview control from agent
  useEffect(() => {
    if (!item.callId) return undefined;

    const subscription = client
      .graphql({
        query: `
        subscription OnVNCPreviewToggle($callId: ID!) {
          onVNCPreviewToggle(CallId: $callId) {
            CallId
            Action
            Success
            RequestedBy
          }
        }
      `,
        variables: { callId: item.callId },
      })
      .subscribe({
        next: (message) => {
          const control = message?.data?.onVNCPreviewToggle;
          if (control) {
            if (control.Success) {
              const shouldShow = control.Action === 'open';
              setShowVNCPreview(shouldShow);
              logger.info(`VNC preview ${control.Action} by ${control.RequestedBy || 'agent'}`);
            }
          }
        },
        error: (err) => {
          logger.error('VNC control subscription error:', err);
        },
      });

    return () => {
      subscription.unsubscribe();
    };
  }, [item.callId]);

  return (
    <SpaceBetween size="s">
      <CallAttributes item={item} setToolsOpen={setToolsOpen} getCallDetailsFromCallIds={getCallDetailsFromCallIds} />

      {/* Show VNC Preview in place of Summary during active meeting if VP is available */}
      {showVNCPreview && vpData && vpData.vncReady && item.recordingStatusLabel === IN_PROGRESS_STATUS ? (
        <VNCViewer
          vpId={vpData.id}
          vncEndpoint={vpData.vncEndpoint}
          status={vpData.status}
          manualActionType={vpData.manualActionType}
          manualActionMessage={vpData.manualActionMessage}
          manualActionTimeoutSeconds={vpData.manualActionTimeoutSeconds}
          manualActionStartTime={vpData.manualActionStartTime}
          compact
          onOpenNewTab={() => window.open(`#/virtual-participant/${vpData.id}`, '_blank')}
          showHeader
        />
      ) : (
        <CallSummary
          item={item}
          // Conditional on the context actually supplying the writer. Three
          // separate components build the calls context by enumerating its
          // fields, and a control wired to a missing one would render and then
          // throw on the click rather than not being offered.
          onSave={sendUpdateCallSummary ? saveCallSummary : undefined}
          onRegenerate={sendRegenerateCallSummary ? regenerateCallSummaryForItem : undefined}
          readOnly={speakerNamesReadOnly}
        />
      )}

      <AgendaPanel
        item={item}
        onSave={sendUpdateCallAgenda ? (text) => sendUpdateCallAgenda(item.callId, text, user?.username) : undefined}
        onDerive={sendDeriveCallAgenda ? () => sendDeriveCallAgenda(item.callId) : undefined}
        readOnly={speakerNamesReadOnly}
      />

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
      <SpeakerIdentitiesPanel
        item={item}
        segments={transcriptSegmentsForCall}
        setToolsOpen={setToolsOpen}
        onSave={saveSpeakerIdentities}
        onIdentify={identifySpeakers}
        readOnly={speakerNamesReadOnly}
        isLive={item.recordingStatusLabel === IN_PROGRESS_STATUS}
      />
      <CallCostPanel
        callId={item.callId}
        refreshKey={`${item.recordingStatusLabel || ''}|${(item.callSummaryText || '').length}|${
          item.callSummaryState || ''
        }`}
      />
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
        speakerIdentities={speakerIdentities}
        onRenameSpeaker={handleRenameSpeaker}
        readOnly={speakerNamesReadOnly}
      />
    </SpaceBetween>
  );
};

export default CallPanel;
