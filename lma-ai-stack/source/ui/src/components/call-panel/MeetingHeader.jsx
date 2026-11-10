/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The top of the meeting page: what the meeting was, in one compact block,
 * above the tabs.
 *
 * The title is the meeting's name, without the time stamp its source appended
 * to the Meeting ID (common/meeting-header.js); the full ID is in the title's
 * tooltip, one click away on the copy button, and on the Details tab. The line
 * under it gives the status, the start time in the viewer's own time zone (the
 * UTC value in its tooltip), the length, the platform when a Virtual
 * Participant joined it, the owner, the consent setting that applied ("Consent
 * Opt-in · granted", or "Consent enforcement off"), which opens the consent
 * record, and a cost chip that opens the itemised estimate. Then the people
 * heard in it, from the transcript and the names given to its voices.
 *
 * The actions are the meeting's: Assistant, which opens and closes the
 * meeting assistant's panel (when the assistant is enabled), Share, Download
 * (the summary and the transcript, each as Markdown, Word or text, the
 * transcript as Excel too, or copied as Markdown), Diagnose (the troubleshooting
 * agent's diagnosis of this meeting, for Admins; troubleshooting/DiagnoseButton.jsx)
 * and, under the "more" menu, Delete. Share and Delete are the meeting list's own
 * dialogs (common/meeting-controls.js).
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Badge,
  Box,
  Button,
  ButtonDropdown,
  Container,
  CopyToClipboard,
  Header,
  Popover,
  SpaceBetween,
  StatusIndicator,
  ToggleButton,
} from '@cloudscape-design/components';

import useCallsContext from '../../contexts/calls';
import { shareModal, deleteModal } from '../common/meeting-controls';
import { InfoLink } from '../common/info-link';
import { DONE_STATUS } from '../common/get-recording-status';
import DiagnoseButton from '../troubleshooting/DiagnoseButton';
import { formatDuration, formatMeetingStart, meetingTitle } from '../common/meeting-header';
import { consentSummary } from '../common/meeting-context';
import { parseSummaryState, summaryStatusText } from '../common/summary-state';
import { CallCostBreakdown, costSummaryText } from './CallCostPanel';
import { ASSISTANT_PANEL_ID } from './MeetingAssistantPanel';
import {
  SUMMARY_ACTION_ITEMS,
  TRANSCRIPT_DOWNLOAD_ITEMS,
  runSummaryAction,
  runTranscriptDownload,
} from './meeting-downloads';

/** How long the outcome of a copy to the clipboard stays beside the Download menu. */
const COPIED_VISIBLE_MS = 4000;

/** How many speakers are named in the header before the rest are counted. */
export const MAX_SPEAKER_CHIPS = 6;

const Meta = ({ label, children, title = undefined }) => (
  <Box variant="span" fontSize="body-s">
    <span title={title}>
      <Box variant="span" color="text-body-secondary" fontSize="body-s">
        {`${label} `}
      </Box>
      {children}
    </span>
  </Box>
);

Meta.propTypes = {
  label: PropTypes.string.isRequired,
  children: PropTypes.node.isRequired,
  title: PropTypes.string,
};

const COST_SHAPE = PropTypes.shape({
  // eslint-disable-next-line react/forbid-prop-types
  estimate: PropTypes.object,
  loading: PropTypes.bool,
  error: PropTypes.string,
  refresh: PropTypes.func,
});

const CostChip = ({ cost }) => {
  const text = costSummaryText(cost, { short: true });
  return (
    <Box variant="span" fontSize="body-s">
      <Box variant="span" color="text-body-secondary" fontSize="body-s">
        {'Est. cost '}
      </Box>
      <Popover
        header="Estimated cost"
        size="large"
        position="bottom"
        triggerType="text"
        content={
          <SpaceBetween size="s">
            <Box float="right">
              <Button iconName="refresh" variant="inline-link" loading={cost.loading} onClick={cost.refresh}>
                Refresh
              </Button>
            </Box>
            <CallCostBreakdown estimate={cost.estimate} error={cost.error} />
          </SpaceBetween>
        }
      >
        <span data-testid="meeting-cost-chip">{text}</span>
      </Popover>
    </Box>
  );
};

CostChip.propTypes = {
  cost: COST_SHAPE.isRequired,
};

const MeetingHeader = ({
  item,
  durationMillis,
  platform = '',
  speakers = [],
  cost,
  consentState = null,
  onShowConsent = undefined,
  callTranscriptPerCallId,
  getCallDetailsFromCallIds = undefined,
  setToolsOpen = undefined,
  onShowSpeakers = undefined,
  onSelectSpeaker = undefined,
  assistant = undefined,
}) => {
  const { calls } = useCallsContext();
  const consent = consentState?.consent || null;
  const consentInfo = consentSummary(consentState);
  const [deleteRequest, setDeleteRequest] = useState(0);
  const controls = { calls, selectedItems: [item], loading: false, getCallDetailsFromCallIds };

  const title = meetingTitle(item.callId);
  const started = formatMeetingStart(item.initiationTimeStamp);
  const hasSummary = Boolean(item.callSummaryText);
  const summaryUnavailableReason =
    summaryStatusText(parseSummaryState(item.callSummaryState)) || 'This meeting has no summary yet.';
  // As before the move: a transcript is offered for download once the meeting
  // has ended, and not after the Retention section has deleted it.
  const canDownloadTranscript = item.recordingStatusLabel === DONE_STATUS && !item.transcriptDeletedAt;
  let transcriptReason;
  if (item.transcriptDeletedAt) transcriptReason = 'The transcript was deleted.';
  else if (!canDownloadTranscript) transcriptReason = 'Available once the meeting has ended.';

  const downloadItems = [
    {
      text: 'Summary',
      items: SUMMARY_ACTION_ITEMS.map((entry) => ({
        ...entry,
        id: `summary:${entry.id}`,
        ariaLabel: `Summary: ${entry.text}`,
        disabled: !hasSummary,
        disabledReason: hasSummary ? undefined : summaryUnavailableReason,
      })),
    },
    {
      text: 'Transcript',
      items: TRANSCRIPT_DOWNLOAD_ITEMS.map((entry) => ({
        ...entry,
        id: `transcript:${entry.id}`,
        ariaLabel: `Transcript: ${entry.text}`,
        disabled: !canDownloadTranscript,
        disabledReason: transcriptReason,
      })),
    },
  ];

  // What a copy to the clipboard came to, shown beside the menu for a moment.
  const [copied, setCopied] = useState(null);
  useEffect(() => {
    if (!copied) return undefined;
    const timer = setTimeout(() => setCopied(null), COPIED_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [copied]);

  const onDownload = async ({ detail }) => {
    const [kind, id] = detail.id.split(':');
    let done = true;
    if (kind === 'summary') done = await runSummaryAction(id, item, consent);
    else if (kind === 'transcript') done = await runTranscriptDownload(id, callTranscriptPerCallId, item);
    if (id === 'copy') {
      const what = kind === 'summary' ? 'Summary' : 'Transcript';
      setCopied(
        done
          ? { type: 'success', text: `${what} copied as Markdown` }
          : { type: 'error', text: `${what} could not be copied` },
      );
    }
  };

  const shownSpeakers = speakers.slice(0, MAX_SPEAKER_CHIPS);
  const hiddenSpeakers = speakers.length - shownSpeakers.length;

  return (
    <Container
      header={
        <Header
          variant="h2"
          info={setToolsOpen ? <InfoLink onFollow={() => setToolsOpen(true)} /> : undefined}
          actions={
            <SpaceBetween size="xs" direction="horizontal">
              {assistant && (
                <ToggleButton
                  ref={assistant.buttonRef}
                  iconName="contact"
                  pressed={assistant.open}
                  onChange={({ detail }) => assistant.onToggle(detail.pressed)}
                  ariaControls={ASSISTANT_PANEL_ID}
                >
                  Assistant
                  {assistant.unread && (
                    <>
                      <span className="meeting-assistant-unread" aria-hidden="true" />
                      <span className="meeting-assistant-visually-hidden">, new answer</span>
                    </>
                  )}
                </ToggleButton>
              )}
              {shareModal({ ...controls, triggerText: 'Share' })}
              {copied && (
                <Box nativeAttributes={{ role: 'status', 'aria-live': 'polite' }}>
                  <StatusIndicator type={copied.type}>{copied.text}</StatusIndicator>
                </Box>
              )}
              <ButtonDropdown items={downloadItems} onItemClick={onDownload}>
                Download
              </ButtonDropdown>
              <DiagnoseButton callId={item.callId} resourceLabel={title} />
              <ButtonDropdown
                variant="icon"
                ariaLabel="More actions"
                items={[{ id: 'delete', text: 'Delete meeting', iconName: 'remove' }]}
                onItemClick={({ detail }) => {
                  if (detail.id === 'delete') setDeleteRequest(Date.now());
                }}
              />
              {deleteModal({ ...controls, hideTrigger: true, openRequest: deleteRequest })}
            </SpaceBetween>
          }
        >
          <span title={item.callId} data-testid="meeting-title">
            {title}
          </span>{' '}
          <CopyToClipboard
            variant="icon"
            textToCopy={item.callId}
            copyButtonAriaLabel="Copy the Meeting ID"
            copySuccessText="Meeting ID copied"
            copyErrorText="The Meeting ID could not be copied"
          />
        </Header>
      }
    >
      <SpaceBetween size="s">
        <SpaceBetween size="l" direction="horizontal" alignItems="center">
          <StatusIndicator type={item.recordingStatusIcon}>{item.recordingStatusLabel}</StatusIndicator>
          {started && (
            <Meta label="Started" title={`${item.initiationTimeStamp} (UTC)`}>
              <span data-testid="meeting-start">{started}</span>
            </Meta>
          )}
          <Meta label="Duration">
            <span data-testid="meeting-duration">{formatDuration(durationMillis)}</span>
          </Meta>
          {platform && (
            <Meta label="Platform">
              <span>{platform}</span>
            </Meta>
          )}
          {item.owner && (
            <Meta label="Owner">
              <span>{item.owner}</span>
            </Meta>
          )}
          {consentInfo.known && (
            // The consent setting that applied, and where the meeting stands
            // with it; it opens the consent record on the Details tab.
            <Box variant="span" fontSize="body-s" data-testid="meeting-consent-indicator">
              {consentInfo.enforced && (
                <Box variant="span" color="text-body-secondary" fontSize="body-s">
                  {'Consent '}
                </Box>
              )}
              {onShowConsent ? (
                <Button
                  variant="inline-link"
                  onClick={onShowConsent}
                  ariaLabel={
                    consentInfo.enforced
                      ? `Consent: ${consentInfo.text}. Show the consent record`
                      : 'Consent enforcement off. Show the consent setting'
                  }
                >
                  {consentInfo.text}
                </Button>
              ) : (
                consentInfo.text
              )}
            </Box>
          )}
          <CostChip cost={cost} />
        </SpaceBetween>
        {shownSpeakers.length > 0 && (
          <SpaceBetween size="xxs" direction="horizontal" alignItems="center">
            <Box variant="span" color="text-body-secondary" fontSize="body-s">
              Speakers
            </Box>
            {shownSpeakers.map((speaker) =>
              // A speaker's chip opens that speaker in the Speakers panel on
              // the Details tab, where its voices are named.
              onSelectSpeaker ? (
                <button
                  key={speaker.key}
                  type="button"
                  className="meeting-speaker-chip"
                  aria-label={`${speaker.label}: show in the Speakers panel`}
                  title="Show in the Speakers panel"
                  onClick={() => onSelectSpeaker(speaker.key)}
                >
                  <Badge color="grey">{speaker.label}</Badge>
                </button>
              ) : (
                <Badge key={speaker.key} color="grey">
                  {speaker.label}
                </Badge>
              ),
            )}
            {hiddenSpeakers > 0 && (
              <Box variant="span" color="text-body-secondary" fontSize="body-s">
                {`+${hiddenSpeakers} more`}
              </Box>
            )}
            {onShowSpeakers && (
              <Button variant="inline-link" onClick={onShowSpeakers}>
                Name speakers
              </Button>
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
};

MeetingHeader.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
  durationMillis: PropTypes.number.isRequired,
  platform: PropTypes.string,
  /** One per name the transcript shows (headerSpeakers): `key` is the voice the Speakers panel opens at. */
  speakers: PropTypes.arrayOf(PropTypes.shape({ key: PropTypes.string, label: PropTypes.string })),
  cost: COST_SHAPE.isRequired,
  // What useMeetingConsent returns: the consent record, for the header's
  // indicator and the summary downloads.
  // eslint-disable-next-line react/forbid-prop-types
  consentState: PropTypes.object,
  onShowConsent: PropTypes.func,
  // eslint-disable-next-line react/forbid-prop-types
  callTranscriptPerCallId: PropTypes.object.isRequired,
  getCallDetailsFromCallIds: PropTypes.func,
  setToolsOpen: PropTypes.func,
  onShowSpeakers: PropTypes.func,
  /** Opens one speaker in the Speakers panel, by its `key`; the chips are plain badges without it. */
  onSelectSpeaker: PropTypes.func,
  /** The Assistant button: absent when the meeting assistant is not enabled. */
  assistant: PropTypes.shape({
    open: PropTypes.bool.isRequired,
    unread: PropTypes.bool,
    onToggle: PropTypes.func.isRequired,
    // eslint-disable-next-line react/forbid-prop-types
    buttonRef: PropTypes.object,
  }),
};

export default MeetingHeader;
