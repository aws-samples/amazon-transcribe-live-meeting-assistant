/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting's chat attachments, by the chat line (SegmentId) that carried them,
 * read through getChatAttachments: the same access check as the meeting, with
 * signed links valid for an hour. See docs/virtual-participant.md, "Chat
 * attachments".
 *
 * An attachment is announced after its chat line, once the Virtual Participant
 * has stored it, and its AI title and summary arrive after that, so the list is
 * read again every POLL_MS while a chat line names an attachment that has not
 * arrived yet or an attachment is still waiting for its title, up to
 * MAX_POLLS times for each new chat line that names one. It is read again every
 * REFRESH_MS as well, before the thumbnails' links lapse. Opening an attachment
 * always reads it again first, for a fresh link.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getChatAttachments from '../graphql/queries/getChatAttachments';

const logger = new ConsoleLogger('useChatAttachments');

export const POLL_MS = 10_000;
export const MAX_POLLS = 30;
export const REFRESH_MS = 45 * 60_000;
/** The note the Virtual Participant adds to a chat line for each attachment. */
export const ATTACHMENT_NOTE = '(attachment: ';

/** Every attachment of a meeting, page by page. */
export const loadChatAttachments = async (callId, attachmentId = null) => {
  const client = generateClient();
  const found = [];
  let nextToken = null;
  do {
    // eslint-disable-next-line no-await-in-loop -- a meeting rarely has a second page
    const response = await client.graphql({
      query: getChatAttachments,
      variables: { callId, attachmentId, nextToken },
    });
    const page = response?.data?.getChatAttachments || {};
    found.push(...(page.ChatAttachments || []).filter(Boolean));
    nextToken = attachmentId ? null : page.nextToken;
  } while (nextToken);
  return found;
};

/** The attachments by SegmentId, each line's in time order. */
export const groupBySegment = (attachments) => {
  const groups = {};
  (attachments || []).forEach((attachment) => {
    if (!attachment?.SegmentId) return;
    (groups[attachment.SegmentId] ||= []).push(attachment);
  });
  Object.values(groups).forEach((list) => list.sort((a, b) => `${a.AttachmentId}`.localeCompare(`${b.AttachmentId}`)));
  return groups;
};

/** Whether anything is still to arrive: a line's attachment, or a title being written. */
export const attachmentsPending = (bySegment, segmentIdsWithAttachments) =>
  (segmentIdsWithAttachments || []).some((id) => !bySegment[id]) ||
  Object.values(bySegment).some((list) => list.some((a) => a.Status === 'STORED' && a.Summarize && !a.SummaryState));

/**
 * @param {string} callId the meeting
 * @param {string[]} segmentIdsWithAttachments the chat lines whose text names an attachment
 * @param {Function} load reads the meeting's attachments (replaceable in tests)
 * @returns {{ bySegment: object, open: Function }} the attachments by line, and a function that opens one
 */
const useChatAttachments = (callId, segmentIdsWithAttachments = [], load = loadChatAttachments) => {
  const [bySegment, setBySegment] = useState({});
  const pollsRef = useRef(0);
  const lineKey = useMemo(() => [...segmentIdsWithAttachments].sort().join('|'), [segmentIdsWithAttachments]);

  const refresh = useCallback(async () => {
    if (!callId) return;
    try {
      setBySegment(groupBySegment(await load(callId)));
    } catch (error) {
      logger.debug('could not read the chat attachments', error);
    }
  }, [callId, load]);

  // Read when the meeting is opened, and whenever a new line names an attachment.
  useEffect(() => {
    pollsRef.current = 0;
    refresh();
  }, [refresh, lineKey]);

  const pending = attachmentsPending(bySegment, lineKey ? lineKey.split('|') : []);
  useEffect(() => {
    if (!pending || pollsRef.current >= MAX_POLLS) return undefined;
    const timer = setTimeout(() => {
      pollsRef.current += 1;
      refresh();
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [pending, bySegment, refresh]);

  useEffect(() => {
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  /**
   * Reads the attachment again for a fresh link, and follows it. S3 serves the
   * link as a download (Content-Disposition: attachment), so the page stays
   * where it is; following it in place rather than in a new window means no
   * popup blocker stops it after the read.
   */
  const open = useCallback(
    async (attachmentId) => {
      try {
        const [attachment] = await load(callId, attachmentId);
        if (attachment?.DownloadUrl) window.location.assign(attachment.DownloadUrl);
      } catch (error) {
        logger.error('could not open the chat attachment', error);
      }
    },
    [callId, load],
  );

  return { bySegment, open };
};

export default useChatAttachments;
