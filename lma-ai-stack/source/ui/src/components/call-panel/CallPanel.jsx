/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Grid,
  Header,
  Input,
  Link,
  Modal,
  Popover,
  Select,
  SpaceBetween,
  StatusIndicator,
  Tabs,
  TextContent,
  Textarea,
  Toggle,
  ToggleButton,
} from '@cloudscape-design/components';
import ReactMarkdown from 'react-markdown';
import { TranslateClient, TranslateTextCommand } from '@aws-sdk/client-translate';
import { StandardRetryStrategy } from '@aws-sdk/middleware-retry';
import markdownRehypePlugins from '../common/markdown-plugins';
import { getMarkdownSummary, getSummarySection, summarySections } from '../common/summary';
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
  buildSpeakerRoster,
  identityKeyFor,
  parseSpeakerIdentities,
  resolveSpeaker,
  serializeSpeakerIdentities,
  speakerGroups,
  speakerRowLabel,
  withSpeakerName,
} from '../common/speaker-identities';
import { speakerChipLayout, withSpeakerMode } from '../common/speaker-modes';
import SpeakerIdentitiesPanel from './SpeakerIdentitiesPanel';
import SpeakerAssignmentModal from './SpeakerAssignmentModal';
import TranscriptSpeakerChips from './TranscriptSpeakerChips';
import AgendaPanel from './AgendaPanel';
import ConsentPanel from './ConsentPanel';
import ConsentPauseAlert from './ConsentPauseAlert';
import CaptureContextPanel, { CONSENT_SETTING_ID } from './CaptureContextPanel';
import { consentEnforced } from '../common/meeting-context';
import TranscriptDeletionNotice from './TranscriptDeletionNotice';
import useMeetingConsent from '../../hooks/use-meeting-consent';
import CallCostPanel from './CallCostPanel';
import MeetingHeader from './MeetingHeader';
import MeetingAttributes from './MeetingAttributes';
import MeetingAssistantPanel from './MeetingAssistantPanel';
import useAssistantPanel from '../../hooks/use-assistant-panel';
import { NodeSlot, useDetachedNode } from './NodeSlot';
import { TAB_DETAILS, TAB_SCREEN_SHARE, TAB_SUMMARY, TAB_TRANSCRIPT } from './meeting-tabs';
import SummarySections from './SummarySections';
import useMeetingTab from '../../hooks/use-meeting-tab';
import useCallCost from '../../hooks/use-call-cost';

import VideoTimeline from '../video-timeline';
import MeetingChapters from '../meeting-chapters/MeetingChapters';
import { chaptersFromSummary } from '../common/chapters';
import { parseAgendaItems } from '../common/agenda';
import { formatDurationClock, meetingDurationMillis, platformLabel } from '../common/meeting-header';
import { frameIdSeconds, scrollTranscriptToTime } from '../common/video-keyframes';
import {
  SEEK_STEP_SECONDS,
  chooseMeetingMedia,
  clockFor,
  defaultMediaPreference,
  currentSegmentId,
  playerShortcut,
  recordingSecondsForFrame,
  sameAnchors,
  toRecordingSeconds,
} from '../meeting-player/playback-sync';
import { usePlaybackSelector, usePlaybackStore } from '../meeting-player/playback-store';
import MeetingPlayer from '../meeting-player';
import {
  passesTranscriptFilter,
  rehypeMarkMatches,
  speakerFilterOptions,
  stepMatch,
  textMatches,
} from '../common/transcript-search';
import useSettingsContext from '../../contexts/settings';

import { IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { InfoLink } from '../common/info-link';
import { getWeightedSentimentLabel } from '../common/sentiment';

import { SentimentFluctuationChart, SentimentPerQuarterChart } from './sentiment-charts';

import './CallPanel.css';
import { SentimentTrendIcon } from '../sentiment-trend-icon/SentimentTrendIcon';
import { SentimentIcon } from '../sentiment-icon/SentimentIcon';
import useAppContext from '../../contexts/app';
import useUserGroups from '../../hooks/use-user-groups';
import awsExports from '../../aws-exports';
import useCallsContext from '../../contexts/calls';
import VNCViewer from '../virtual-participant-layout/VNCViewer';
import { listVirtualParticipants, onUpdateVirtualParticipant } from '../../graphql/queries/virtualParticipantQueries';
import useChatKeyframeLinks from '../../hooks/use-chat-keyframe-links';
import { isSharedWith } from '../common/shared-with';
import useStrandsChatBridge from '../../hooks/use-strands-chat-bridge';
import { isChatChannel, isSpeechChannel } from '../common/channels';
import ChatSegment from '../chat-segment/ChatSegment';

const client = generateClient();
const logger = new ConsoleLogger('CallPanel');

// comprehend PII types
const piiTypesSplitRegEx = new RegExp(`\\[(${COMPREHEND_PII_TYPES.join('|')})\\]`);

const MAXIMUM_ATTEMPTS = 100;
const MAXIMUM_RETRY_DELAY = 1000;

const PAUSE_TO_MERGE_IN_SECONDS = 1;

/**
 * The height of the transcript and of the assistant beside it: the viewport
 * less the page chrome above them (navigation, breadcrumbs, the meeting header,
 * the tabs and the transcript's own header), and never less than a readable
 * box. The transcript keeps its own scroll, rather than using the page's,
 * because following a live meeting means holding the newest line in view while
 * the reader can still scroll back, and that is a property of one scrolling
 * box; the assistant's frame needs a fixed height beside it in any case.
 */
const TRANSCRIPT_HEIGHT = 'max(360px, calc(100vh - 420px))';

/** Below this width of the page, the assistant panel lies over the tabs instead of beside them. */
export const ASSISTANT_OVERLAY_BELOW_PX = 900;
/** The assistant panel's least height, and its distance from the top navigation and the window's foot. */
const ASSISTANT_MIN_HEIGHT_PX = 420;
const ASSISTANT_GAP_PX = 12;

/* eslint-disable react/prop-types, react/destructuring-assignment */
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

// Where the consent record is on the Details tab, for the links that open it.
const CONSENT_RECORD_ID = 'meeting-consent-record';

const EDIT_AFTER_MEETING_REASON =
  'Available once the meeting has ended, so the end-of-meeting summary cannot replace your text.';

// Exported for its own tests: regenerating is an asynchronous run whose
// progress and failure are read back from the meeting rather than from the
// mutation's answer, and that handover is worth driving directly.
// eslint-disable-next-line arrow-body-style
export const CallSummary = ({
  item,
  onSave,
  onRegenerate,
  readOnly,
  omitSections = [],
  keyframes = 'image',
  onKeyframeSelect = undefined,
}) => {
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

  // The meeting page shows some sections in their own place -- the chapters as
  // a list, the agenda coverage under the agenda -- and leaves them out here.
  // The downloads are in the page header's Download menu.
  const omitKey = omitSections.join('|');
  const sections = useMemo(() => summarySections(item.callSummaryText, omitSections), [item.callSummaryText, omitKey]);

  return (
    <Container
      header={
        <Header
          variant="h4"
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
      {editing ? (
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
        <SummarySections
          callId={item.callId}
          sections={sections}
          keyframes={keyframes}
          onKeyframeSelect={onKeyframeSelect}
        />
      )}
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

// The translation of a segment's text, once it has one; '' until then.
const translationOf = ({ segmentId, targetLanguage, translateOn }, translateCache) => {
  if (!translateOn || !targetLanguage) return '';
  return translateCache?.[segmentId.concat('-', targetLanguage)]?.translated ?? '';
};

const TranscriptContent = ({ segment, translateCache, query = '' }) => {
  const { transcript, segmentId, channel } = segment;
  // The search's words marked in the segment's own text (not its translation).
  const transcriptPlugins = useMemo(
    () => (query ? [...markdownRehypePlugins, rehypeMarkMatches(query)] : markdownRehypePlugins),
    [query],
  );

  const result = translationOf(segment, translateCache);

  const transcriptPiiSplit = transcript.split(piiTypesSplitRegEx);

  const transcriptComponents = transcriptPiiSplit.map((t, i) => {
    if (COMPREHEND_PII_TYPES.includes(t)) {
      // eslint-disable-next-line react/no-array-index-key
      return <Badge key={`${segmentId}-pii-${i}`} color="red">{`${t}`}</Badge>;
    }

    let className = '';
    const text = t;
    const translatedText = result;

    switch (channel) {
      case 'AGENT_ASSISTANT':
      case 'MEETING_ASSISTANT':
        className = 'transcript-segment-agent-assist';
        break;
      case 'LOCAL':
      case 'REMOTE':
        break;
      default:
        break;
    }

    return (
      // prettier-ignore
      // eslint-disable-next-line react/no-array-index-key
      <TextContent key={`${segmentId}-text-${i}`} color="red" className={className}>
        <ReactMarkdown rehypePlugins={transcriptPlugins}>{text.trim()}</ReactMarkdown>
        {/* react-markdown 9 and later take no className: wrapped, so the translation keeps its colour. */}
        <div className="translated-text">
          <ReactMarkdown rehypePlugins={markdownRehypePlugins}>{translatedText.trim()}</ReactMarkdown>
        </div>
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

const TranscriptSegment = ({
  segment,
  translateCache,
  enableSentimentAnalysis,
  onRenameSpeaker,
  readOnly,
  onSeek = null,
  query = '',
}) => {
  const { channel } = segment;

  // A message typed in the meeting chat: at one moment, the one it was read.
  if (isChatChannel(channel)) {
    return (
      <Grid className="transcript-segment" disableGutters gridDefinition={[{ colspan: 1 }, { colspan: 10 }]}>
        <div className="sentiment-image" />
        <ChatSegment
          segment={segment}
          time={getTimestampFromSeconds(segment.startTime)}
          onSeek={onSeek}
          query={query}
          translation={translationOf(segment, translateCache)}
        />
      </Grid>
    );
  }

  const times = `${getTimestampFromSeconds(segment.startTime)} - ${getTimestampFromSeconds(segment.endTime)}`;

  let displayChannel = `${segment.channel}`;
  let channelClass = '';

  if (channel === 'LOCAL' || channel === 'REMOTE') {
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
          {onRenameSpeaker && !readOnly && (channel === 'LOCAL' || channel === 'REMOTE') ? (
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
          {onSeek ? (
            // With a recording to play, the time plays it from this line.
            <Button
              variant="inline-link"
              ariaLabel={`Play from ${getTimestampFromSeconds(segment.startTime)}`}
              onClick={() => onSeek(segment.startTime)}
            >
              {times}
            </Button>
          ) : (
            <TextContent>{times}</TextContent>
          )}
        </SpaceBetween>
        <TranscriptContent segment={segment} translateCache={translateCache} query={query} />
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
  // Each chat message is its own row.
  && !isChatChannel(current.channel)
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

// Whether a segment is somebody speaking: not the assistants' answers, and not
// a message typed in the meeting chat.
const isSpeechSegment = (segment) => isSpeechChannel(segment?.channel);

// The segments the transcript shows, of those it holds: not the chat
// assistant's (it has its own panel), and only complete ones.
const isDisplayedSegment = (s) =>
  // prettier-ignore
  s?.segmentId
  && s?.createdAt
  && (s.agentTranscript === undefined || s.agentTranscript || s.channel !== 'LOCAL')
  && s.channel !== 'CHAT_ASSISTANT';

const rowIdFor = (s) => `${s.segmentId}-${s.createdAt}`;

// How long after the page scrolls the transcript itself a scroll event is
// taken to be that scroll rather than the reader's. A smooth scroll over a long
// transcript takes most of a second.
const PROGRAMMATIC_SCROLL_MS = 1000;

/**
 * One line of the transcript. Memoised: while a recording plays, the
 * transcript re-renders each time the segment being heard changes, and only
 * the two rows whose highlight moved need to.
 */
const TranscriptRow = React.memo(
  ({
    segment,
    current,
    match,
    query,
    translateCache,
    enableSentimentAnalysis,
    participantName,
    onRenameSpeaker,
    readOnly,
    onSeek,
  }) => {
    const classes = ['transcript-row'];
    if (current) classes.push('transcript-row-current');
    if (match) classes.push('transcript-row-match');
    if (match === 'active') classes.push('transcript-row-match-active');
    return (
      <div
        className={classes.join(' ')}
        data-start-time={segment.startTime}
        data-row-id={rowIdFor(segment)}
        aria-current={current ? 'true' : undefined}
      >
        <TranscriptSegment
          segment={segment}
          translateCache={translateCache}
          enableSentimentAnalysis={enableSentimentAnalysis}
          participantName={participantName}
          onRenameSpeaker={onRenameSpeaker}
          readOnly={readOnly}
          onSeek={onSeek}
          query={match ? query : ''}
        />
      </div>
    );
  },
);
TranscriptRow.displayName = 'TranscriptRow';

/**
 * The part of the transcript's box that can be seen: all of it, less the top
 * when the page has been scrolled and the player, which stays at the top of
 * the window, lies over it.
 */
const visibleBounds = (container) => {
  const box = container.getBoundingClientRect();
  const player = container.closest('.transcript-column')?.querySelector('.meeting-player');
  const cover = player ? player.getBoundingClientRect().bottom : -Infinity;
  return { top: Math.max(box.top, Math.min(cover, box.bottom)), bottom: box.bottom };
};

/** Whether a row is wholly inside the visible part of its scrolling container. */
const rowInView = (container, row) => {
  const { top, bottom } = visibleBounds(container);
  const r = row.getBoundingClientRect();
  return r.top >= top && r.bottom <= bottom;
};

const findRow = (container, rowId) =>
  [...(container?.querySelectorAll('[data-row-id]') || [])].find((row) => row.getAttribute('data-row-id') === rowId);

/**
 * Search and speaker filter for the transcript, above its lines. The search
 * marks the segments holding every word typed and steps through them; the
 * speaker chips limit the transcript to some speakers.
 */
const TranscriptToolbar = ({
  query,
  onQueryChange,
  matchCount,
  activeMatch,
  onStep,
  speakerNames,
  selectedSpeakers,
  onSpeakersChange,
  hasChat = false,
  showChat = true,
  onShowChatChange = () => {},
  editableSpeakers = null,
  onEditSpeaker = undefined,
  chipEntries = null,
  onSetSpeakerMode = undefined,
  onRenameVoices = undefined,
}) => {
  let countText = '';
  if (query.trim()) {
    countText = matchCount ? `${activeMatch + 1 > 0 ? activeMatch + 1 : 0} of ${matchCount}` : 'No matches';
  }
  const toggleSpeaker = (name, pressed) =>
    onSpeakersChange(pressed ? [...selectedSpeakers, name] : selectedSpeakers.filter((n) => n !== name));
  const canEdit = Boolean(onEditSpeaker && editableSpeakers?.size);
  // The chips, grouped by platform name; one plain chip per name when the
  // caller has not grouped them.
  const entries = chipEntries ?? speakerNames.map((name) => ({ type: 'name', name, keys: [] }));
  const hasGroups = entries.some((entry) => entry.type === 'group');
  return (
    <div className="transcript-toolbar" data-testid="transcript-toolbar">
      <div className="transcript-search">
        <Input
          type="search"
          value={query}
          placeholder="Search the transcript"
          ariaLabel="Search the transcript"
          onChange={({ detail }) => onQueryChange(detail.value)}
          onKeyDown={({ detail }) => {
            if (detail.key === 'Enter') onStep(detail.shiftKey ? -1 : 1);
          }}
        />
        <Button
          variant="icon"
          iconName="angle-up"
          ariaLabel="Previous match"
          disabled={!matchCount}
          onClick={() => onStep(-1)}
        />
        <Button
          variant="icon"
          iconName="angle-down"
          ariaLabel="Next match"
          disabled={!matchCount}
          onClick={() => onStep(1)}
        />
        <span className="transcript-search-count" aria-live="polite" data-testid="transcript-search-count">
          {countText}
        </span>
      </div>
      {(speakerNames.length > 1 || hasChat || hasGroups || (canEdit && speakerNames.length > 0)) && (
        <div className="transcript-speaker-filter" role="group" aria-label="Show speakers">
          {(speakerNames.length > 1 || canEdit || hasGroups) && (
            <>
              {speakerNames.length > 1 && (
                <ToggleButton
                  pressed={!selectedSpeakers.length}
                  onChange={() => onSpeakersChange([])}
                  ariaLabel="All speakers"
                >
                  All
                </ToggleButton>
              )}
              <TranscriptSpeakerChips
                entries={entries}
                filterable={speakerNames.length > 1}
                selectedSpeakers={selectedSpeakers}
                onToggleSpeaker={toggleSpeaker}
                editableSpeakers={canEdit ? editableSpeakers : null}
                onEditSpeaker={canEdit ? onEditSpeaker : undefined}
                onSetMode={onSetSpeakerMode}
                onRenameVoices={onRenameVoices}
              />
            </>
          )}
          {/* The meeting chat is shown or hidden on its own, whichever speakers are chosen. */}
          {hasChat && (
            <span className="transcript-chat-filter">
              <ToggleButton
                iconName="contact"
                pressed={showChat}
                onChange={({ detail }) => onShowChatChange(detail.pressed)}
                ariaLabel="Chat messages"
              >
                Chat
              </ToggleButton>
            </span>
          )}
        </div>
      )}
    </div>
  );
};

const CallInProgressTranscript = ({
  item,
  callTranscriptPerCallId,
  autoScroll,
  scrollRequest,
  translateClient,
  targetLanguage,
  agentTranscript,
  translateOn,
  enableSentimentAnalysis,
  speakerIdentities,
  onRenameSpeaker,
  readOnly,
  active = true,
  playback = null,
  onSeek = null,
  onSaveSpeakerIdentities = undefined,
}) => {
  const containerRef = useRef();
  // The speaker whose chip opened the speaker modal, or null.
  const [editingSpeaker, setEditingSpeaker] = useState(null);
  const [turnByTurnSegments, setTurnByTurnSegments] = useState([]);
  const [translateCache, setTranslateCache] = useState({});
  const [cacheSeen, setCacheSeen] = useState({});
  const [lastUpdated, setLastUpdated] = useState(Date.now());
  const [updateFlag, setUpdateFlag] = useState(false);
  const [userHasScrolled, setUserHasScrolled] = useState(false);
  const [query, setQuery] = useState('');
  const [activeMatch, setActiveMatch] = useState(-1);
  const [selectedSpeakers, setSelectedSpeakers] = useState([]);
  const [showChat, setShowChat] = useState(true);
  // Until when a scroll event is the page's own scrolling, not the reader's.
  const programmaticUntilRef = useRef(0);

  const isLive = item.recordingStatusLabel === IN_PROGRESS_STATUS;
  // After the meeting, with a recording to play, Auto-scroll keeps the line
  // being heard in view; during it, the newest line.
  const followPlayback = Boolean(playback && onSeek && autoScroll && !isLive);

  // Every channel the meeting has: REMOTE, LOCAL, the assistants, the voice-tone
  // readings and MEETING_CHAT. Which of them are shown is isDisplayedSegment's call.
  const { callId } = item;
  const transcriptsForThisCallId = callTranscriptPerCallId[callId] || {};
  const transcriptChannels = Object.keys(transcriptsForThisCallId);

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

  // Whether each platform name's voices are one person or several, decided over
  // the whole transcript (speaker-modes.js). Recomputed as segments arrive, so
  // a live meeting's lines are relabelled when a second voice becomes a real
  // share of a name's speech, and whenever the map changes.
  const groups = useMemo(
    () => speakerGroups(getSegments(), speakerIdentities),
    [callTranscriptPerCallId, callId, speakerIdentities],
  );

  const getTurnByTurnSegments = () =>
    transcriptChannels
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
        // A chat message stays under the sender the meeting's chat gave: it is
        // not a voice, so the names given to voices do not apply to it.
        if (isChatChannel(t.channel)) return t;
        t.speakerKey = identityKeyFor(t, speakerIdentities);
        // remoteParticipantName is the remote side's name, so it substitutes for the
        // placeholder on the REMOTE channel only. The download path has always
        // scoped it this way; the other readers now match.
        const resolved = resolveSpeaker(
          t,
          speakerIdentities,
          t.channel === 'REMOTE' ? item.remoteParticipantName : undefined,
          groups,
        );
        t.speaker = resolved.name;
        // The platform name this line was shown under as one person or
        // several, for the grouped speaker chips.
        t.speakerGroup = resolved.group;
        t.speakerLocked = resolved.locked;
        // A name given to this segment alone, not to its voice.
        t.speakerOverridden = resolved.overridden;
        // An inferred name that nobody has confirmed. Rendered differently
        // below, because showing a guess as indistinguishable from a name a
        // person typed is the one thing that would make this actively harmful.
        t.speakerProposed = resolved.status === 'proposed';

        return t;
      })
      .filter(isDisplayedSegment);

  // What is shown: the speakers chosen, then the segments holding the search.
  const speakerNames = useMemo(() => speakerFilterOptions(turnByTurnSegments, isSpeechSegment), [turnByTurnSegments]);
  // A chosen speaker who is no longer in the transcript (renamed) stops filtering.
  const activeSpeakers = useMemo(
    () => selectedSpeakers.filter((name) => speakerNames.includes(name)),
    [selectedSpeakers, speakerNames],
  );
  const hasChat = useMemo(() => turnByTurnSegments.some((s) => isChatChannel(s.channel)), [turnByTurnSegments]);
  // The voices each speaker's chip stands for: usually one, and several when
  // voices that were never merged were given the same name. A name given to a
  // single segment stands for no voice, so it is not edited from its chip.
  const voiceKeysByName = useMemo(() => {
    const byName = new Map();
    turnByTurnSegments.forEach((s) => {
      if (!isSpeechSegment(s) || s.speakerOverridden || !s.speakerKey) return;
      const name = `${s.speaker ?? ''}`.trim();
      if (!byName.has(name)) byName.set(name, new Set());
      byName.get(name).add(s.speakerKey);
    });
    return byName;
  }, [turnByTurnSegments]);
  const editableSpeakers = useMemo(
    () => (onSaveSpeakerIdentities && !readOnly ? new Set(voiceKeysByName.keys()) : null),
    [voiceKeysByName, onSaveSpeakerIdentities, readOnly],
  );
  // The chips, grouped by platform name, with each group's One person /
  // Several people toggle.
  const chipEntries = useMemo(
    () => speakerChipLayout(turnByTurnSegments.filter(isSpeechSegment), groups),
    [turnByTurnSegments, groups],
  );
  const canWriteSpeakers = Boolean(onSaveSpeakerIdentities && !readOnly);
  // One write each: the chosen mode, or the name for every voice behind a chip
  // (an empty name returns them to "Voice N").
  const setSpeakerMode = (key, mode) =>
    onSaveSpeakerIdentities(serializeSpeakerIdentities(withSpeakerMode(speakerIdentities, key, mode)));
  const renameVoices = (keys, name) =>
    onSaveSpeakerIdentities(
      serializeSpeakerIdentities(keys.reduce((map, key) => withSpeakerName(map, key, name), speakerIdentities)),
    );
  // The meeting's voices as the Speakers panel lists them, read when the modal opens.
  const roster = useMemo(
    () =>
      editingSpeaker === null ? [] : buildSpeakerRoster(getSegments(), speakerIdentities, item.remoteParticipantName),
    [editingSpeaker, callTranscriptPerCallId, speakerIdentities, item.remoteParticipantName],
  );
  const editingKeys = editingSpeaker === null ? null : voiceKeysByName.get(editingSpeaker);
  const editingRows = editingKeys ? roster.filter((row) => editingKeys.has(row.key)) : [];
  const visibleSegments = useMemo(
    () =>
      turnByTurnSegments.filter((s) =>
        passesTranscriptFilter(s, { speakers: activeSpeakers, showChat }, isSpeechSegment),
      ),
    [turnByTurnSegments, activeSpeakers, showChat],
  );
  const matchIds = useMemo(
    () => (query.trim() ? visibleSegments.filter((s) => textMatches(s.transcript, query)).map(rowIdFor) : []),
    [visibleSegments, query],
  );
  const matchSet = useMemo(() => new Set(matchIds), [matchIds]);
  const activeMatchId = activeMatch >= 0 ? matchIds[activeMatch] : null;

  // The line being heard, from the player's position. Re-renders the
  // transcript only when that line changes. A chat message is never what is
  // being heard: it was typed, not said, so the highlight and the scrolling
  // that follow the recording pass over it.
  const playbackRows = useMemo(
    () =>
      visibleSegments
        .filter((s) => !isChatChannel(s.channel))
        .map((s) => ({ id: rowIdFor(s), startTime: s.startTime, endTime: s.endTime })),
    [visibleSegments],
  );
  const selectCurrent = useCallback(
    (state) => (state.engaged ? currentSegmentId(playbackRows, state.seconds) : null),
    [playbackRows],
  );
  const currentRowId = usePlaybackSelector(onSeek ? playback : null, selectCurrent);
  const selectSeekCount = useCallback((state) => state.seekCount || 0, []);
  const seekCount = usePlaybackSelector(onSeek ? playback : null, selectSeekCount);

  const scrollToRow = useCallback((row) => {
    const container = containerRef.current;
    if (!container || !row) return;
    programmaticUntilRef.current = Date.now() + PROGRAMMATIC_SCROLL_MS;
    const box = container.getBoundingClientRect();
    const visible = visibleBounds(container);
    const top = row.getBoundingClientRect().top - box.top + container.scrollTop;
    // A third of the way down what can be seen, so what comes next is in view too.
    const target = top - (visible.top - box.top) - (visible.bottom - visible.top) / 3;
    if (container.scrollTo) container.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  }, []);

  // Detect when user manually scrolls
  const handleScroll = (e) => {
    const container = e.target;
    if (isLive) {
      const threshold = 50; // pixels from bottom
      const isAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
      if (!isAtBottom && autoScroll) {
        setUserHasScrolled(true);
      } else if (isAtBottom) {
        setUserHasScrolled(false);
      }
      return;
    }
    // While a recording plays, scrolling away stops the transcript following
    // it, until Resume or the next move of the recording.
    if (Date.now() < programmaticUntilRef.current) return;
    if (followPlayback && playback?.get().playing) setUserHasScrolled(true);
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
  ]);

  // Move to the segment at a requested time (a key frame or a chapter was
  // selected). During a live meeting this pauses following the live end, so
  // the transcript stays there; after it, the recording has been moved to the
  // same time, so following it carries on from there.
  // The transcript is on its own tab, and a hidden element has no layout to
  // scroll by, so a request made while another tab is shown is kept and
  // carried out when the Transcript tab is opened.
  const appliedScrollRef = useRef(null);
  useEffect(() => {
    if (!scrollRequest || !active || appliedScrollRef.current === scrollRequest.at) return;
    appliedScrollRef.current = scrollRequest.at;
    programmaticUntilRef.current = Date.now() + PROGRAMMATIC_SCROLL_MS;
    const row = scrollTranscriptToTime(containerRef.current, scrollRequest.seconds);
    if (row && isLive) setUserHasScrolled(true);
    if (!isLive) setUserHasScrolled(false);
  }, [scrollRequest, active]);

  useEffect(() => {
    // prettier-ignore
    if (
      active
      && isLive
      && autoScroll
      && !userHasScrolled
      && containerRef.current
    ) {
      scrollTranscriptToBottom();
    }
  }, [
    active,
    turnByTurnSegments,
    autoScroll,
    userHasScrolled,
    item.recordingStatusLabel,
    targetLanguage,
    agentTranscript,
    translateOn,
    scrollTranscriptToBottom,
  ]);

  // An explicit move of the recording (a click on a line, a chapter, the seek
  // bar, the arrow keys) is where the reader wants to be: follow it again.
  useEffect(() => {
    if (seekCount) setUserHasScrolled(false);
  }, [seekCount]);

  // Keep the line being heard in view, unless the reader has scrolled away.
  useEffect(() => {
    if (!followPlayback || userHasScrolled || !active || !currentRowId) return;
    const container = containerRef.current;
    const row = findRow(container, currentRowId);
    if (row && !rowInView(container, row)) scrollToRow(row);
  }, [currentRowId, followPlayback, userHasScrolled, active, scrollToRow]);

  // A new search starts at its first match.
  useEffect(() => {
    setActiveMatch(query.trim() && matchIds.length ? 0 : -1);
  }, [query]);
  useEffect(() => {
    if (activeMatch >= matchIds.length) setActiveMatch(matchIds.length ? 0 : -1);
  }, [matchIds.length]);
  useEffect(() => {
    if (!activeMatchId || !active) return;
    scrollToRow(findRow(containerRef.current, activeMatchId));
  }, [activeMatchId, active, scrollToRow]);

  const stepSearch = (step) => {
    if (!matchIds.length) return;
    // Reading the matches is a move away from the line being heard.
    if (followPlayback && playback?.get().playing) setUserHasScrolled(true);
    setActiveMatch((index) => stepMatch(matchIds.length, index, step));
  };

  const resumeFollowing = () => {
    setUserHasScrolled(false);
    if (isLive) scrollTranscriptToBottom();
  };

  let pausedNotice = null;
  if (userHasScrolled && autoScroll && isLive) pausedNotice = 'Auto-scroll paused - scroll to bottom to resume';
  if (userHasScrolled && followPlayback) pausedNotice = 'Following the recording is paused';

  return (
    <>
      <TranscriptToolbar
        query={query}
        onQueryChange={setQuery}
        matchCount={matchIds.length}
        activeMatch={activeMatch}
        onStep={stepSearch}
        speakerNames={speakerNames}
        selectedSpeakers={activeSpeakers}
        onSpeakersChange={setSelectedSpeakers}
        hasChat={hasChat}
        showChat={showChat}
        onShowChatChange={setShowChat}
        editableSpeakers={editableSpeakers}
        onEditSpeaker={editableSpeakers ? setEditingSpeaker : undefined}
        chipEntries={chipEntries}
        onSetSpeakerMode={canWriteSpeakers ? setSpeakerMode : undefined}
        onRenameVoices={canWriteSpeakers ? renameVoices : undefined}
      />
      {editingRows.length > 0 && (
        <SpeakerAssignmentModal
          speaker={editingSpeaker}
          rows={editingRows}
          roster={roster}
          identities={speakerIdentities}
          onSave={onSaveSpeakerIdentities}
          onDismiss={() => setEditingSpeaker(null)}
          readOnly={readOnly}
        />
      )}
      {/* The notice lies over the lines rather than among them: shown and
          hidden among them, it would move them, and the browser's scroll
          anchoring would then scroll the box -- which reads as the reader
          scrolling, and shows the notice again. */}
      <div className="transcript-scroll-frame">
        {pausedNotice && (
          <div className="transcript-follow-paused" role="status">
            {`⚠️ ${pausedNotice}`}
            <Button
              onClick={resumeFollowing}
              variant="inline-link"
              iconName="angle-down"
              style={{ marginLeft: '10px', fontSize: '12px' }}
            >
              Resume
            </Button>
          </div>
        )}
        <div
          ref={containerRef}
          onScroll={handleScroll}
          data-testid="transcript-scroll"
          style={{
            overflowY: 'auto',
            height: TRANSCRIPT_HEIGHT,
            maxHeight: TRANSCRIPT_HEIGHT,
            paddingLeft: '10px',
            paddingTop: '5px',
            paddingRight: '10px',
          }}
        >
          {visibleSegments.length === 0 && turnByTurnSegments.length > 0 && (
            <Box color="text-body-secondary" padding="s">
              {!showChat && !activeSpeakers.length
                ? 'No lines to show with the chat hidden.'
                : 'No lines from the speakers chosen.'}
            </Box>
          )}
          <ColumnLayout borders="horizontal" columns={1}>
            {visibleSegments.map((s) => {
              const rowId = rowIdFor(s);
              let match = null;
              if (matchSet.has(rowId)) match = rowId === activeMatchId ? 'active' : 'match';
              return (
                <TranscriptRow
                  key={rowId}
                  segment={s}
                  current={rowId === currentRowId}
                  match={match}
                  query={query}
                  translateCache={translateCache}
                  enableSentimentAnalysis={enableSentimentAnalysis}
                  participantName={item.remoteParticipantName}
                  onRenameSpeaker={onRenameSpeaker}
                  readOnly={readOnly}
                  onSeek={onSeek}
                />
              );
            })}
          </ColumnLayout>
        </div>
      </div>
    </>
  );
};

const LANGUAGE_OPTIONS = LANGUAGE_CODES.filter(({ value }) => value).map(({ value, label }) => ({ value, label }));

const CallTranscriptContainer = ({
  setToolsOpen,
  item,
  callTranscriptPerCallId,
  translateClient,
  enableSentimentAnalysis,
  speakerIdentities,
  onRenameSpeaker,
  readOnly,
  scrollRequest,
  active = true,
  player = null,
  playback = null,
  onSeek = null,
  onSaveSpeakerIdentities = undefined,
}) => {
  // Auto-scroll follows the newest line while the meeting runs, and the line
  // being heard once it has ended and there is a recording to play; it is on
  // by default in both. With neither there is nothing for it to follow.
  const canFollow = item.recordingStatusLabel === IN_PROGRESS_STATUS || Boolean(onSeek);
  const [autoScroll, setAutoScroll] = useState(canFollow);
  const [autoScrollDisabled, setAutoScrollDisabled] = useState(!canFollow);

  const [translateOn, setTranslateOn] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState(localStorage.getItem('targetLanguage') || '');
  const [agentTranscript] = useState(true);

  const handleLanguageSelect = ({ detail }) => {
    const value = detail.selectedOption?.value || '';
    setTargetLanguage(value);
    localStorage.setItem('targetLanguage', value);
  };

  useEffect(() => {
    setAutoScrollDisabled(!canFollow);
    setAutoScroll(canFollow);
  }, [canFollow]);

  const selectedLanguage = LANGUAGE_OPTIONS.find((option) => option.value === targetLanguage) || null;

  return (
    <div className="transcript-column">
      {player}
      <Container
        fitHeight="true"
        disableContentPaddings
        header={
          <Header
            variant="h4"
            info={<InfoLink onFollow={() => setToolsOpen(true)} />}
            actions={
              // The transcript's downloads are in the page header's Download menu.
              <SpaceBetween direction="horizontal" size="m" alignItems="center">
                <Toggle
                  onChange={({ detail }) => setAutoScroll(detail.checked)}
                  checked={autoScroll}
                  disabled={autoScrollDisabled}
                >
                  Auto-scroll
                </Toggle>
                <Toggle onChange={({ detail }) => setTranslateOn(detail.checked)} checked={translateOn}>
                  Translate
                </Toggle>
                {translateOn && (
                  <Select
                    selectedOption={selectedLanguage}
                    onChange={handleLanguageSelect}
                    options={LANGUAGE_OPTIONS}
                    placeholder="Choose a language"
                    filteringType="auto"
                    ariaLabel="Translation language"
                    expandToViewport
                  />
                )}
              </SpaceBetween>
            }
          >
            Transcript
          </Header>
        }
      >
        <TranscriptDeletionNotice
          deletedAt={item.transcriptDeletedAt}
          scheduledFor={item.transcriptDeletionScheduledFor}
        />
        <CallInProgressTranscript
          item={item}
          callTranscriptPerCallId={callTranscriptPerCallId}
          autoScroll={autoScroll}
          scrollRequest={scrollRequest}
          translateClient={translateClient}
          targetLanguage={targetLanguage}
          agentTranscript={agentTranscript}
          translateOn={translateOn}
          enableSentimentAnalysis={enableSentimentAnalysis}
          speakerIdentities={speakerIdentities}
          onRenameSpeaker={onRenameSpeaker}
          readOnly={readOnly}
          active={active}
          playback={playback}
          onSeek={onSeek}
          onSaveSpeakerIdentities={onSaveSpeakerIdentities}
        />
      </Container>
    </div>
  );
};

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
                <strong>Remote Avg Sentiment:</strong>
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
                <strong>Remote Sentiment Trend:</strong>
              </Box>
              <div>
                <SentimentTrendIcon trend={item.callerSentimentTrendLabel} />
              </div>
            </div>
          </SpaceBetween>
          <SpaceBetween size="xs">
            <div>
              <Box margin={{ bottom: 'xxxs' }} color="text-label">
                <strong>Local Avg Sentiment:</strong>
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
                <strong>Local Sentiment Trend:</strong>
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

export const CallPanel = ({
  item,
  callTranscriptPerCallId,
  setToolsOpen,
  getCallDetailsFromCallIds,
  tabInUrl = false,
  assistantDefaultOpen = undefined,
}) => {
  const { currentCredentials, user } = useAppContext();
  const { isAdmin: isAdminUser } = useUserGroups();

  const { settings } = useSettingsContext();
  const [collapseSentiment, setCollapseSentiment] = useState(false);

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
    !isAdminUser && item.owner !== user?.username && !isSharedWith(item.sharedWith, user?.username);

  // The consent record, for the Consent panel and the summary downloads. Read
  // with its own query, which signs the evidence links, and kept current by
  // its own subscription.
  const meetingConsent = useMeetingConsent(item.callId);

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

  const isLive = item.recordingStatusLabel === IN_PROGRESS_STATUS;

  // The meeting assistant, in a panel beside the tabs on every tab
  // (MeetingAssistantPanel). Whether it is open is remembered per user, and a
  // live meeting opens with it open the first time (use-assistant-panel.js).
  // Read here rather than once at load, so a test can set the flag.
  const assistantEnabled = import.meta.env.VITE_ENABLE_AGENT_ASSIST === 'true';
  // Whether the panel lies over the tabs (a narrow page) rather than beside
  // them; null until the page has been measured (see the layout effect below).
  const [assistantOverlays, setAssistantOverlays] = useState(null);
  const assistant = useAssistantPanel({
    enabled: assistantEnabled,
    callId: item.callId,
    isLive,
    username: user?.username,
    defaultOpen: assistantDefaultOpen,
    besideTabs: assistantOverlays === null ? null : !assistantOverlays,
  });
  const setAssistantOpen = assistant.setOpen;
  // A new value each time the viewer opens the panel: the focus goes into the composer.
  const [assistantFocusRequest, setAssistantFocusRequest] = useState(0);
  // An answer finished while the panel was closed; shown on the header button.
  const [assistantUnread, setAssistantUnread] = useState(false);
  const assistantOpenRef = useRef(assistant.open);
  assistantOpenRef.current = assistant.open;
  const assistantButtonRef = useRef(null);
  useEffect(() => {
    if (assistant.open) setAssistantUnread(false);
  }, [assistant.open]);
  useEffect(() => {
    setAssistantUnread(false);
  }, [item.callId]);
  const toggleAssistant = useCallback(
    (open) => {
      setAssistantOpen(open);
      if (open) setAssistantFocusRequest((previous) => previous + 1);
    },
    [setAssistantOpen],
  );
  const closeAssistant = useCallback(
    ({ returnFocus = false } = {}) => {
      setAssistantOpen(false);
      if (returnFocus) assistantButtonRef.current?.focus();
    },
    [setAssistantOpen],
  );
  const onAnswerComplete = useCallback(() => {
    if (!assistantOpenRef.current) setAssistantUnread(true);
  }, []);

  // Answers the meeting assistant chat frame, and its requests for frames of
  // the shared screen; the embeddable chat panel uses the same two hooks.
  useStrandsChatBridge(item.callId, { onAnswerComplete });
  useChatKeyframeLinks(item.callId);

  // Whether the Virtual Participant's live view is shown, at the top of the
  // Transcript tab. Opened and closed from the assistant's settings menu and
  // by the assistant itself (the VNC preview subscription below).
  const [showVNCPreview, setShowVNCPreview] = useState(false);

  // How many key frames the strip has loaded. It decides whether the meeting
  // has a Screen share tab, and the tab's label.
  const [keyframes, setKeyframes] = useState({ count: 0, more: false, loaded: false });
  useEffect(() => {
    setKeyframes({ count: 0, more: false, loaded: false });
  }, [item.callId]);
  const hasKeyframes = keyframes.count > 0;
  const [activeTab, setActiveTab] = useMeetingTab({ inUrl: tabInUrl, isLive, hasKeyframes });
  // Opening the live view also opens the tab it is on, wherever it was asked for.
  const setVncPreview = useCallback(
    (show) => {
      setShowVNCPreview(show);
      if (show) setActiveTab(TAB_TRANSCRIPT);
    },
    [setActiveTab],
  );
  const setVncPreviewRef = useRef(setVncPreview);
  setVncPreviewRef.current = setVncPreview;
  const timelineNode = useDetachedNode();

  // The Virtual Participant that joined this meeting, if one did: for the
  // platform in the header and, once its browser can be watched, the live view.
  const [meetingVp, setMeetingVp] = useState(null);
  const [loadingVP, setLoadingVP] = useState(false);
  const vpData = meetingVp?.vncReady ? meetingVp : null;

  // The meeting's one player, above the transcript (meeting-player/). It plays
  // the video recording when there is one and the audio recording otherwise,
  // and nothing while the meeting is live; the viewer can switch between them
  // when there are both. The video recording's clock against the
  // transcript's comes from the key frames (each carries both); empty until
  // the strip has loaded frames that do.
  const [clockAnchors, setClockAnchors] = useState([]);
  // The viewer's own choice of recording; null until they make one.
  const [chosenMedia, setChosenMedia] = useState(null);
  useEffect(() => {
    setClockAnchors([]);
    setChosenMedia(null);
  }, [item.callId]);
  // Without a choice, a Virtual Participant's video with no key frames to
  // line it up gives way to the audio recording (defaultMediaPreference).
  const mediaDefault = defaultMediaPreference({
    hasBoth: Boolean(item.recordingUrl && item.videoRecordingUrl),
    isVirtualParticipant: Boolean(meetingVp),
    framesLoaded: keyframes.loaded,
    hasAnchors: clockAnchors.length > 0,
  });
  const media = chooseMeetingMedia({
    recordingUrl: item.recordingUrl,
    videoRecordingUrl: item.videoRecordingUrl,
    isLive,
    prefer: chosenMedia || mediaDefault.prefer,
  });
  const audioDefaultReason = !chosenMedia && media?.kind === 'audio' ? mediaDefault.reason : null;
  const mediaKind = media?.kind || null;
  const canPlayRecording = Boolean(media);
  const playerRef = useRef(null);
  const playback = usePlaybackStore();

  const onClockAnchors = useCallback((anchors) => {
    setClockAnchors((previous) => (sameAnchors(previous, anchors) ? previous : anchors));
  }, []);
  const clock = useMemo(() => clockFor(mediaKind, clockAnchors), [mediaKind, clockAnchors]);
  const clockRef = useRef(clock);
  clockRef.current = clock;

  /**
   * Move the player to a time on the transcript's clock (or to a key frame's
   * own place in the video recording), and play from there when `play`.
   */
  const seekPlayer = useCallback(
    ({ transcriptSeconds = null, frame = null, play = false }) => {
      const player = playerRef.current;
      if (!player || !mediaKind) return;
      const seconds = frame
        ? recordingSecondsForFrame(frame, { kind: mediaKind, clock: clockRef.current })
        : toRecordingSeconds(clockRef.current, transcriptSeconds);
      player.seek(seconds, { play });
    },
    [mediaKind],
  );

  // Requests from the key frame strip, the chapters and the summary's slides:
  // move the transcript to a time, and the player with it. `at` makes a
  // repeated request distinct. `reveal` is an explicit "show me": it opens the
  // Transcript tab, where the player is. Without it the transcript and the
  // player are only moved, ready for when the tab is next opened, and nothing
  // plays out of sight.
  const [transcriptScrollRequest, setTranscriptScrollRequest] = useState(null);
  const [frameSelectRequest, setFrameSelectRequest] = useState(null);
  const showInTranscript = useCallback(
    (seconds, { reveal = false } = {}) => {
      setTranscriptScrollRequest({ seconds, at: Date.now() });
      seekPlayer({ transcriptSeconds: seconds, play: false });
      if (reveal) setActiveTab(TAB_TRANSCRIPT);
    },
    [setActiveTab, seekPlayer],
  );
  // "Play recording from here" on a key frame (reveal), or a frame selected in
  // the strip (no reveal: positioned, not played).
  const playRecordingFrom = useCallback(
    (frame, { reveal = false } = {}) => {
      if (reveal) {
        setTranscriptScrollRequest({ seconds: Number(frame?.Timestamp) || 0, at: Date.now() });
        setActiveTab(TAB_TRANSCRIPT);
      }
      seekPlayer({ frame, play: reveal });
    },
    [setActiveTab, seekPlayer],
  );
  // A slide named in the summary: open the Screen share tab at its frame, and
  // move the player to it for when the recording is played from there.
  const showKeyframe = useCallback(
    (frameId) => {
      setFrameSelectRequest({ frameId, at: Date.now() });
      setActiveTab(TAB_SCREEN_SHARE);
      const seconds = frameIdSeconds(frameId);
      if (seconds !== null) {
        setTranscriptScrollRequest({ seconds, at: Date.now() });
        seekPlayer({ transcriptSeconds: seconds, play: false });
      }
    },
    [setActiveTab, seekPlayer],
  );
  // "Show in transcript" under a frame the assistant showed: as the same
  // action on the Screen share tab, from the time in the frame's id. Where the
  // panel lies over the page, it is closed so the transcript can be seen; the
  // remembered choice is left as it was.
  const assistantOverlaysRef = useRef(assistantOverlays);
  assistantOverlaysRef.current = assistantOverlays;
  const showChatFrameInTranscript = useCallback(
    (frameId) => {
      const seconds = frameIdSeconds(frameId);
      if (seconds === null) return;
      showInTranscript(seconds, { reveal: true });
      if (assistantOverlaysRef.current) setAssistantOpen(false, { remember: false });
    },
    [showInTranscript, setAssistantOpen],
  );
  const onFramesChange = useCallback(({ count, more, loaded }) => {
    setKeyframes((previous) =>
      previous.count === count && previous.more === more && previous.loaded === Boolean(loaded)
        ? previous
        : { count, more, loaded: Boolean(loaded) },
    );
  }, []);

  // Chapters from the summary, on the transcript's clock.
  const chapters = useMemo(() => chaptersFromSummary(item.callSummaryText), [item.callSummaryText]);
  // A chapter selected on the Summary tab: the Transcript tab at that time, the
  // recording playing from it.
  const selectChapter = useCallback(
    (chapter) => {
      showInTranscript(chapter.seconds, { reveal: true });
      seekPlayer({ transcriptSeconds: chapter.seconds, play: true });
    },
    [showInTranscript, seekPlayer],
  );
  // A line of the transcript: play from its start.
  const playFromTranscript = useCallback(
    (seconds) => seekPlayer({ transcriptSeconds: seconds, play: true }),
    [seekPlayer],
  );
  // The Details tab's "Play" on a recording: the Transcript tab, playing that
  // recording from where the player is.
  // A change of recording -- chosen, or the default settling -- keeps the
  // place. Read here, while rendering: the player resets the position when
  // its recording changes, and its effects run before this component's.
  const pendingSwitchRef = useRef(null);
  const playOnTranscriptTab = useCallback(
    (kind) => {
      setActiveTab(TAB_TRANSCRIPT);
      const state = playback.get();
      const seconds = state.engaged ? state.seconds : 0;
      if (kind && kind !== mediaKind) {
        // Played once the other recording has loaded, from the same place.
        pendingSwitchRef.current = { seconds, playing: true };
        setChosenMedia(kind);
        return;
      }
      playerRef.current?.seek(toRecordingSeconds(clockRef.current, seconds), { play: true });
    },
    [setActiveTab, playback, mediaKind],
  );
  const shownMediaRef = useRef({ callId: item.callId, url: media?.url });
  if (shownMediaRef.current.url !== media?.url) {
    const { seconds, playing, engaged } = playback.get();
    const sameMeeting = shownMediaRef.current.callId === item.callId;
    if (!sameMeeting) {
      pendingSwitchRef.current = null;
    } else if (!pendingSwitchRef.current && engaged && shownMediaRef.current.url) {
      pendingSwitchRef.current = { seconds, playing };
    }
    shownMediaRef.current = { callId: item.callId, url: media?.url };
  }
  useEffect(() => {
    const pending = pendingSwitchRef.current;
    pendingSwitchRef.current = null;
    if (pending) seekPlayer({ transcriptSeconds: pending.seconds, play: pending.playing });
  }, [media?.url]);

  // Nothing plays out of sight: leaving the Transcript tab pauses the player.
  useEffect(() => {
    if (activeTab !== TAB_TRANSCRIPT) playerRef.current?.pause();
  }, [activeTab]);

  // Space plays or pauses, Left and Right move 5 seconds: on the Transcript
  // tab, with a recording, while focus is on nothing in particular or within
  // this panel and not in a field (playerShortcut). Within this panel, so the
  // meeting list's split panel does not take the keys of the list beside it.
  const panelRef = useRef(null);
  useEffect(() => {
    if (activeTab !== TAB_TRANSCRIPT || !mediaKind) return undefined;
    const onKeyDown = (event) => {
      const { target } = event;
      const inPanel = target === document.body || panelRef.current?.contains(target);
      const action = inPanel ? playerShortcut(event) : null;
      const player = playerRef.current;
      if (!action || !player) return;
      event.preventDefault();
      if (action === 'toggle') player.toggle();
      if (action === 'back') player.nudge(-SEEK_STEP_SECONDS);
      if (action === 'forward') player.nudge(SEEK_STEP_SECONDS);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [activeTab, mediaKind]);

  // Fetch Virtual Participant data for this call
  useEffect(() => {
    const fetchVPData = async () => {
      if (!item.callId) return;

      try {
        setLoadingVP(true);
        const result = await client.graphql({ query: listVirtualParticipants });

        const vps = result.data.listVirtualParticipants || [];
        const matchingVP = vps.find((vp) => vp.CallId === item.callId);
        setMeetingVp(matchingVP || null);
        if (matchingVP) logger.info('Found VP for call:', matchingVP);
      } catch (error) {
        logger.error('Error fetching VP data:', error);
        setMeetingVp(null);
      } finally {
        setLoadingVP(false);
      }
    };

    fetchVPData();
  }, [item.callId]);

  // Subscribe to VP updates
  useEffect(() => {
    if (!meetingVp?.id) return undefined;

    const subscription = client.graphql({ query: onUpdateVirtualParticipant }).subscribe({
      next: (message) => {
        const updated = message?.data?.onUpdateVirtualParticipant;
        if (updated && updated.id === meetingVp.id) {
          setMeetingVp((prev) => ({
            ...prev,
            ...updated,
          }));
          logger.info('VP updated:', updated);
        }
      },
      error: (err) => logger.error('VP subscription error:', err),
    });

    return () => subscription.unsubscribe();
  }, [meetingVp?.id]);

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
              setVncPreviewRef.current(shouldShow);
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

  // The header's figures. The length is read from the transcript as well as
  // from the stored value (see meetingDurationMillis), and the downloads carry
  // the same figure the page shows.
  const durationMillis = meetingDurationMillis(item, transcriptSegmentsForCall);
  const displayItem = useMemo(
    () => ({ ...item, conversationDurationTimeStamp: formatDurationClock(durationMillis) }),
    [item, durationMillis],
  );
  const platform = platformLabel(meetingVp?.meetingPlatform);
  const roster = buildSpeakerRoster(transcriptSegmentsForCall, speakerIdentities, item.remoteParticipantName);
  const speakers = roster.map((row) => ({ key: row.key, label: speakerRowLabel(row) }));

  // The consent record is shown on the Details tab when consent was enforced
  // for the meeting (or could not be read); the header's indicator and the
  // paused-transcription alert open it, or the consent setting when it was off.
  const showConsentRecordPanel = Boolean(meetingConsent.error) || consentEnforced(meetingConsent.consent?.Mode);
  const [consentScrollRequest, setConsentScrollRequest] = useState(null);
  const showConsent = useCallback(() => {
    setActiveTab(TAB_DETAILS);
    setConsentScrollRequest({ at: Date.now() });
  }, [setActiveTab]);
  useEffect(() => {
    if (!consentScrollRequest || activeTab !== TAB_DETAILS) return;
    const target =
      document.getElementById(showConsentRecordPanel ? CONSENT_RECORD_ID : CONSENT_SETTING_ID) ||
      document.getElementById(CONSENT_SETTING_ID);
    target?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    setConsentScrollRequest(null);
  }, [consentScrollRequest, activeTab, showConsentRecordPanel]);
  const cost = useCallCost(
    item.callId,
    `${item.recordingStatusLabel || ''}|${(item.callSummaryText || '').length}|${item.callSummaryState || ''}`,
  );

  // The summary's sections that the Summary tab shows in their own place: the
  // chapters as the clickable list, and the agenda coverage under the agenda.
  // Each is left in the summary when there is nothing to show it in instead.
  const agendaItems = parseAgendaItems(item.agenda);
  const agendaCoverage = getSummarySection(item.callSummaryText, 'AGENDA COVERAGE');
  const coverageSaysNoAgenda = /^\s*no agenda was set/i.test(agendaCoverage || '');
  const omitSections = [
    ...(chapters.length ? ['CHAPTERS'] : []),
    ...(agendaCoverage !== null && (agendaItems.length || coverageSaysNoAgenda) ? ['AGENDA COVERAGE'] : []),
  ];

  const showVnc = showVNCPreview && vpData && vpData.vncReady && isLive;
  const sideColumn = chapters.length > 0 || agendaItems.length > 0;

  const agendaPanel = (
    <AgendaPanel
      item={item}
      onSave={sendUpdateCallAgenda ? (text) => sendUpdateCallAgenda(item.callId, text, user?.username) : undefined}
      onDerive={sendDeriveCallAgenda ? () => sendDeriveCallAgenda(item.callId) : undefined}
      readOnly={speakerNamesReadOnly}
      compactWhenEmpty
      coverage={agendaItems.length && !coverageSaysNoAgenda ? agendaCoverage : null}
    />
  );

  const summaryTab = (
    <SpaceBetween size="l">
      {!agendaItems.length && agendaPanel}
      <Grid
        gridDefinition={
          sideColumn ? [{ colspan: { default: 12, m: 8 } }, { colspan: { default: 12, m: 4 } }] : [{ colspan: 12 }]
        }
      >
        <CallSummary
          item={displayItem}
          // Conditional on the context actually supplying the writer. Three
          // separate components build the calls context by enumerating its
          // fields, and a control wired to a missing one would render and then
          // throw on the click rather than not being offered.
          onSave={sendUpdateCallSummary ? saveCallSummary : undefined}
          onRegenerate={sendRegenerateCallSummary ? regenerateCallSummaryForItem : undefined}
          readOnly={speakerNamesReadOnly}
          omitSections={omitSections}
          // The slides the summary names are shown as their thumbnails, read
          // once their section is opened; a click shows one on the Screen
          // share tab when the meeting has one, and otherwise opens it full size.
          keyframes="image"
          onKeyframeSelect={hasKeyframes ? showKeyframe : undefined}
        />
        {sideColumn && (
          <SpaceBetween size="l">
            <MeetingChapters chapters={chapters} onSelect={selectChapter} canPlayRecording={canPlayRecording} />
            {agendaItems.length > 0 && agendaPanel}
          </SpaceBetween>
        )}
      </Grid>
      {enableSentimentAnalysis && (
        <Grid gridDefinition={[{ colspan: 12 }]}>
          <CallStatsContainer
            item={item}
            callTranscriptPerCallId={callTranscriptPerCallId}
            collapseSentiment={collapseSentiment}
            setCollapseSentiment={setCollapseSentiment}
          />
        </Grid>
      )}
    </SpaceBetween>
  );

  const transcript = (
    <SpaceBetween size="l">
      {/* The Virtual Participant's live view, while the meeting runs and it has been opened. */}
      {showVnc && (
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
      )}
      <CallTranscriptContainer
        item={item}
        player={
          media ? (
            <MeetingPlayer
              controllerRef={playerRef}
              kind={media.kind}
              url={media.url}
              clock={clock}
              chapters={chapters}
              store={playback}
              alternative={media.alternative}
              onSwitch={setChosenMedia}
              hint={audioDefaultReason}
            />
          ) : null
        }
        playback={media ? playback : null}
        onSeek={media ? playFromTranscript : null}
        setToolsOpen={setToolsOpen}
        callTranscriptPerCallId={callTranscriptPerCallId}
        translateClient={translateClient}
        enableSentimentAnalysis={enableSentimentAnalysis}
        speakerIdentities={speakerIdentities}
        onRenameSpeaker={handleRenameSpeaker}
        onSaveSpeakerIdentities={sendUpdateSpeakerIdentities ? saveSpeakerIdentities : undefined}
        readOnly={speakerNamesReadOnly}
        scrollRequest={transcriptScrollRequest}
        active={activeTab === TAB_TRANSCRIPT}
      />
    </SpaceBetween>
  );

  const details = (
    <SpaceBetween size="l">
      <MeetingAttributes
        item={item}
        durationMillis={durationMillis}
        platform={platform}
        onPlay={canPlayRecording ? playOnTranscriptTab : undefined}
        audioDefaultReason={audioDefaultReason}
      />
      <CaptureContextPanel
        item={item}
        vp={meetingVp}
        platform={platform}
        roster={roster}
        consentState={meetingConsent}
        onShowConsentRecord={showConsentRecordPanel ? showConsent : undefined}
      />
      <SpeakerIdentitiesPanel
        item={item}
        segments={transcriptSegmentsForCall}
        setToolsOpen={setToolsOpen}
        onSave={saveSpeakerIdentities}
        onIdentify={identifySpeakers}
        readOnly={speakerNamesReadOnly}
        isLive={isLive}
      />
      {showConsentRecordPanel && (
        <div id={CONSENT_RECORD_ID}>
          <ConsentPanel
            state={meetingConsent}
            canEdit={!speakerNamesReadOnly}
            isOwner={!!user?.username && item.owner === user.username}
            isAdmin={!!isAdminUser}
          />
        </div>
      )}
      <CallCostPanel callId={item.callId} state={cost} />
    </SpaceBetween>
  );

  // Every tab stays mounted while another is shown ('eager'): switching tabs
  // keeps the transcript's translations and scroll position, a summary or
  // agenda being edited, and the assistant's conversation.
  const tabs = [
    { id: TAB_SUMMARY, label: 'Summary', content: summaryTab, contentRenderStrategy: 'eager' },
    { id: TAB_TRANSCRIPT, label: 'Transcript', content: transcript, contentRenderStrategy: 'eager' },
    ...(hasKeyframes
      ? [
          {
            id: TAB_SCREEN_SHARE,
            label: `Screen share (${keyframes.count}${keyframes.more ? '+' : ''})`,
            content: <NodeSlot node={timelineNode} />,
            contentRenderStrategy: 'eager',
          },
        ]
      : []),
    { id: TAB_DETAILS, label: 'Details', content: details, contentRenderStrategy: 'eager' },
  ];

  // The assistant panel's place: a column beside the tabs, or, where the page
  // is narrower than ASSISTANT_OVERLAY_BELOW_PX (a phone, the meeting list's
  // split panel at the side, a narrow embed), over the right of the tabs.
  // Measured on the page's own width rather than the window's, because the
  // same page is shown full width, in the split panel and in embeds. Sticky
  // below the application's top navigation, as the player is, and as tall as
  // the window leaves below where it starts on the page, so the composer at
  // its foot is in view without scrolling. Measured again when the page, the
  // header above it (speaker names arriving) or the window change size.
  // Measured before the first paint, because with no choice made the width
  // decides whether the panel starts open.
  const bodyRef = useRef(null);
  const headerRef = useRef(null);
  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body || !assistantEnabled) return undefined;
    const nav = document.getElementById('top-navigation');
    const measure = () => {
      const navHeight = nav ? nav.offsetHeight : 0;
      const pageTop = body.getBoundingClientRect().top + window.scrollY;
      const top = Math.max(pageTop, navHeight + ASSISTANT_GAP_PX);
      setAssistantOverlays(body.clientWidth > 0 && body.clientWidth < ASSISTANT_OVERLAY_BELOW_PX);
      body.style.setProperty('--meeting-assistant-top', `${navHeight}px`);
      body.style.setProperty(
        '--meeting-assistant-height',
        `${Math.max(ASSISTANT_MIN_HEIGHT_PX, window.innerHeight - top - ASSISTANT_GAP_PX)}px`,
      );
    };
    measure();
    window.addEventListener('resize', measure);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(body);
    if (headerRef.current) observer?.observe(headerRef.current);
    if (nav) observer?.observe(nav);
    return () => {
      window.removeEventListener('resize', measure);
      observer?.disconnect();
    };
  }, [assistantEnabled]);

  const bodyClass = [
    'meeting-body',
    assistant.open ? 'meeting-body-assistant-open' : '',
    assistantOverlays ? 'meeting-body-assistant-overlay' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div ref={panelRef}>
      <SpaceBetween size="m">
        <div ref={headerRef}>
          <MeetingHeader
            item={displayItem}
            durationMillis={durationMillis}
            platform={platform}
            speakers={speakers}
            cost={cost}
            consentState={meetingConsent}
            onShowConsent={showConsent}
            callTranscriptPerCallId={callTranscriptPerCallId}
            getCallDetailsFromCallIds={getCallDetailsFromCallIds}
            setToolsOpen={setToolsOpen}
            onShowSpeakers={speakerNamesReadOnly ? undefined : () => setActiveTab(TAB_DETAILS)}
            assistant={
              assistantEnabled
                ? {
                    open: assistant.open,
                    unread: assistantUnread,
                    onToggle: toggleAssistant,
                    buttonRef: assistantButtonRef,
                  }
                : undefined
            }
          />
        </div>
        <ConsentPauseAlert
          isLive={isLive}
          consentState={meetingConsent}
          startedAt={item.initiationTimeStamp}
          onShowRecord={showConsentRecordPanel ? showConsent : undefined}
        />
        <div ref={bodyRef} className={bodyClass}>
          <div className="meeting-main">
            <Tabs
              tabs={tabs}
              activeTabId={activeTab}
              onChange={({ detail }) => setActiveTab(detail.activeTabId)}
              ariaLabel="Meeting"
            />
          </div>
          {/* Created on first opening, then hidden rather than removed, so the chat keeps its conversation. */}
          {assistant.mounted && (
            <MeetingAssistantPanel
              callId={item.callId}
              open={assistant.open}
              onClose={closeAssistant}
              focusRequest={assistantFocusRequest}
              onShowInTranscript={showChatFrameInTranscript}
              showVNCPreview={showVNCPreview}
              setShowVNCPreview={setVncPreview}
              vpData={vpData}
              loadingVP={loadingVP}
            />
          )}
        </div>
        {timelineNode &&
          createPortal(
            <VideoTimeline
              key={item.callId}
              callId={item.callId}
              title={item.callId}
              isLive={isLive}
              canPlayRecording={canPlayRecording}
              onShowInTranscript={showInTranscript}
              onPlayFrom={playRecordingFrom}
              chapters={chapters}
              onClockAnchors={onClockAnchors}
              onFramesChange={onFramesChange}
              selectRequest={frameSelectRequest}
            />,
            timelineNode,
          )}
      </SpaceBetween>
    </div>
  );
};

export default CallPanel;
