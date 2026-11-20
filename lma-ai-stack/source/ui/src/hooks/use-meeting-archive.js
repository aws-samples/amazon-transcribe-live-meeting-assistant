/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A ZIP archive of a meeting's files, built on the server: prepareMeetingArchive
 * starts it and returns at once, and getMeetingArchive is read every POLL_MS
 * until it is READY, with a link valid for an hour, or FAILED, with a message.
 * The archive is a temporary copy, deleted a day after it was written. See
 * docs/web-ui-guide.md.
 *
 * `Contents` is EVERYTHING, KEY_FRAMES or CHAT_ATTACHMENTS.
 */
import { generateClient } from 'aws-amplify/api';

import prepareMeetingArchive from '../graphql/queries/prepareMeetingArchive';
import getMeetingArchive from '../graphql/queries/getMeetingArchive';

export const POLL_MS = 3000;
/** How long the page waits for an archive: the build's own limit is 15 minutes. */
export const MAX_WAIT_MS = 16 * 60 * 1000;

const graphql = (query, variables) => generateClient().graphql({ query, variables });

/** Starts an archive of the meeting; resolves to its first status ({ ArchiveId, Status, ... }). */
export const startMeetingArchive = async (callId, contents, { request = graphql } = {}) => {
  const response = await request(prepareMeetingArchive, { CallId: callId, Contents: contents });
  const archive = response?.data?.prepareMeetingArchive;
  if (!archive?.ArchiveId) throw new Error('the archive could not be started');
  return archive;
};

/** The archive's status now. */
export const readMeetingArchive = async (callId, archiveId, { request = graphql } = {}) => {
  const response = await request(getMeetingArchive, { CallId: callId, ArchiveId: archiveId });
  return response?.data?.getMeetingArchive || null;
};

const wait = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

/**
 * Starts an archive and waits for it: resolves to its last status, READY or
 * FAILED (FAILED with a message after `maxWaitMs`). `onUpdate` is called with
 * each status read; `isCancelled` stops the wait, resolving to null.
 */
export const prepareAndWaitForArchive = async (
  callId,
  contents,
  {
    request = graphql,
    pollMs = POLL_MS,
    maxWaitMs = MAX_WAIT_MS,
    onUpdate = () => {},
    isCancelled = () => false,
    sleep = wait,
  } = {},
) => {
  let archive = await startMeetingArchive(callId, contents, { request });
  onUpdate(archive);
  let waited = 0;
  while (archive.Status === 'PREPARING') {
    if (waited >= maxWaitMs) {
      return { ...archive, Status: 'FAILED', Message: 'The archive took too long to prepare. Try again later.' };
    }
    // eslint-disable-next-line no-await-in-loop -- one status read at a time, POLL_MS apart
    await sleep(pollMs);
    waited += pollMs;
    if (isCancelled()) return null;
    // eslint-disable-next-line no-await-in-loop
    archive = (await readMeetingArchive(callId, archive.ArchiveId, { request })) || archive;
    onUpdate(archive);
  }
  return archive;
};
