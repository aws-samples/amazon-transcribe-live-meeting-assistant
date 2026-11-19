/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page header's Download menu: every file the meeting has, in
 * groups (downloadMenuItems in meeting-downloads.js), and beside the menu what
 * the last action came to -- a copy to the clipboard, or an archive being
 * prepared on the server ("Preparing download…") and then its link.
 *
 * Every file is fetched through a time-limited signed link, after the same
 * access check as the meeting: a recording is signed with the viewer's own
 * credentials, as the Details tab's Open file is; a chat attachment is read
 * again through getChatAttachments for a fresh link, which saves the file
 * rather than showing it; an archive's link comes from getMeetingArchive.
 */
import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { Box, ButtonDropdown, Link, StatusIndicator } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';
import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { parseSummaryState, summaryStatusText } from '../common/summary-state';
import { loadEveryKeyframe, openDeckTab, printDeck } from '../common/slide-deck';
import { meetingTitle } from '../common/meeting-header';
import { loadKeyframePage } from '../../hooks/use-chat-keyframe-links';
import { loadChatAttachments } from '../../hooks/use-chat-attachments';
import { prepareAndWaitForArchive } from '../../hooks/use-meeting-archive';
import {
  downloadMenuItems,
  openSignedFile,
  parseMenuId,
  recordingSessions,
  runSummaryAction,
  runTranscriptDownload,
} from './meeting-downloads';

/** How long the outcome of a copy to the clipboard stays beside the menu. */
const COPIED_VISIBLE_MS = 4000;
/** How often the attachment list is read again while the meeting is going on. */
const ATTACHMENTS_REFRESH_MS = 60_000;

const ARCHIVE_LABELS = {
  EVERYTHING: 'Everything',
  KEY_FRAMES: 'Key frames',
  CHAT_ATTACHMENTS: 'Chat attachments',
};

const MeetingDownloadMenu = ({
  item,
  consent = null,
  speakers = [],
  callTranscriptPerCallId,
  keyframeCount = 0,
  chapters = [],
  loadAttachments = loadChatAttachments,
  prepareArchive = prepareAndWaitForArchive,
  sign = generateS3PresignedUrl,
  openFile = openSignedFile,
  printSlides = undefined,
}) => {
  const { currentCredentials } = useAppContext() || {};
  const { callId } = item;

  // The stored attachments, for an entry each; read when the page opens, and
  // again every minute while the meeting is going on.
  const [attachments, setAttachments] = useState([]);
  const live = item.recordingStatusLabel === IN_PROGRESS_STATUS;
  useEffect(() => {
    let current = true;
    const read = () =>
      loadAttachments(callId)
        .then((found) => {
          if (current) setAttachments(found || []);
        })
        .catch(() => {
          if (current) setAttachments([]);
        });
    read();
    const timer = live ? setInterval(read, ATTACHMENTS_REFRESH_MS) : null;
    return () => {
      current = false;
      if (timer) clearInterval(timer);
    };
  }, [callId, live, loadAttachments]);

  const [notice, setNotice] = useState(null);
  useEffect(() => {
    if (!notice?.fades) return undefined;
    const timer = setTimeout(() => setNotice(null), COPIED_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  // The archive being prepared; a newer request, or leaving the meeting, stops the wait for an older one.
  const [archive, setArchive] = useState(null);
  const archiveRun = useRef(0);
  useEffect(
    () => () => {
      archiveRun.current += 1;
    },
    [callId],
  );

  const hasSummary = Boolean(item.callSummaryText);
  const canDownloadTranscript = item.recordingStatusLabel === DONE_STATUS && !item.transcriptDeletedAt;
  let transcriptReason;
  if (item.transcriptDeletedAt) transcriptReason = 'The transcript was deleted.';
  else if (!canDownloadTranscript) transcriptReason = 'Available once the meeting has ended.';
  const audioUrls = recordingSessions(item.recordingUrl, item.recordingUrls);
  const videoUrls = recordingSessions(item.videoRecordingUrl, item.videoRecordingUrls);

  const items = downloadMenuItems({
    hasSummary,
    summaryReason: summaryStatusText(parseSummaryState(item.callSummaryState)) || 'This meeting has no summary yet.',
    canDownloadTranscript,
    transcriptReason,
    audioUrls,
    videoUrls,
    keyframeCount,
    attachments,
  });

  const startArchive = async (contents) => {
    archiveRun.current += 1;
    const run = archiveRun.current;
    const label = ARCHIVE_LABELS[contents];
    setArchive({ label, Status: 'PREPARING' });
    let result;
    try {
      result = await prepareArchive(callId, contents, {
        isCancelled: () => run !== archiveRun.current,
        onUpdate: (update) => {
          if (run === archiveRun.current) setArchive({ label, ...update });
        },
      });
    } catch (error) {
      result = { Status: 'FAILED', Message: 'The archive could not be prepared.' };
    }
    if (result && run === archiveRun.current) setArchive({ label, ...result });
  };

  const printTheSlides = async () => {
    if (printSlides) {
      await printSlides();
      return;
    }
    const tab = openDeckTab();
    if (!tab) {
      setNotice({ type: 'error', text: 'The browser blocked the new tab. Allow pop-ups for this site.' });
      return;
    }
    try {
      const frames = await loadEveryKeyframe(callId, loadKeyframePage);
      await printDeck(tab, { title: meetingTitle(callId), frames, chapters });
    } catch (error) {
      tab.close();
      setNotice({ type: 'error', text: 'The slides could not be prepared.' });
    }
  };

  const onItemClick = async ({ detail }) => {
    const [kind, rest] = parseMenuId(detail.id);
    if (kind === 'summary' || kind === 'transcript') {
      const done =
        kind === 'summary'
          ? await runSummaryAction(rest, item, consent, { participants: speakers.map((speaker) => speaker.label) })
          : await runTranscriptDownload(rest, callTranscriptPerCallId, item, consent);
      if (rest === 'copy') {
        const what = kind === 'summary' ? 'Summary' : 'Transcript';
        setNotice(
          done
            ? { type: 'success', text: `${what} copied as Markdown`, fades: true }
            : { type: 'error', text: `${what} could not be copied`, fades: true },
        );
      }
    } else if (kind === 'recording') {
      const [type, index] = rest.split(':');
      const url = (type === 'video' ? videoUrls : audioUrls)[Number(index)];
      if (url && !(await openFile(() => sign(url, currentCredentials)))) {
        setNotice({ type: 'error', text: 'The recording could not be opened.' });
      }
    } else if (kind === 'attachment') {
      const reached = await openFile(async () => {
        const [fresh] = await loadAttachments(callId, rest);
        return fresh?.DownloadUrl || null;
      });
      if (!reached) setNotice({ type: 'error', text: 'The attachment could not be downloaded.' });
    } else if (kind === 'slides') {
      await printTheSlides();
    } else if (kind === 'archive' && ARCHIVE_LABELS[rest]) {
      await startArchive(rest);
    }
  };

  return (
    <>
      {notice && (
        <Box nativeAttributes={{ role: 'status', 'aria-live': 'polite' }}>
          <StatusIndicator type={notice.type}>{notice.text}</StatusIndicator>
        </Box>
      )}
      {archive && (
        <Box nativeAttributes={{ role: 'status', 'aria-live': 'polite' }} data-testid="meeting-archive-status">
          {archive.Status === 'PREPARING' && (
            <StatusIndicator type="loading">Preparing download… ({archive.label})</StatusIndicator>
          )}
          {archive.Status === 'READY' && archive.DownloadUrl && (
            <StatusIndicator type="success">
              <Link href={archive.DownloadUrl} external externalIconAriaLabel="(link valid for an hour)">
                Download {archive.FileName || `${archive.label}.zip`}
              </Link>
            </StatusIndicator>
          )}
          {archive.Status === 'FAILED' && (
            <StatusIndicator type="error">{archive.Message || 'The archive could not be prepared.'}</StatusIndicator>
          )}
        </Box>
      )}
      <ButtonDropdown items={items} onItemClick={onItemClick} expandableGroups>
        Download
      </ButtonDropdown>
    </>
  );
};

MeetingDownloadMenu.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  consent: PropTypes.object,
  speakers: PropTypes.arrayOf(PropTypes.shape({ key: PropTypes.string, label: PropTypes.string })),
  // eslint-disable-next-line react/forbid-prop-types
  callTranscriptPerCallId: PropTypes.object.isRequired,
  keyframeCount: PropTypes.number,
  // eslint-disable-next-line react/forbid-prop-types
  chapters: PropTypes.array,
  loadAttachments: PropTypes.func,
  prepareArchive: PropTypes.func,
  sign: PropTypes.func,
  openFile: PropTypes.func,
  printSlides: PropTypes.func,
};

export default MeetingDownloadMenu;
