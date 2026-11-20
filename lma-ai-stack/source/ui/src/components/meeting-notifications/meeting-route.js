/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { matchPath } from 'react-router-dom';
import { CALLS_PATH } from '../../routes/constants';

/** The meeting a meeting page shows, from its path; null on the meeting list and the query page. */
export const meetingCallIdOf = (pathname) => {
  const match = matchPath(`${CALLS_PATH}/:callId`, pathname || '');
  let callId = match?.params?.callId;
  if (!callId) return null;
  // The meeting page reads it decoded (useParams); matchPath leaves it as written.
  try {
    callId = decodeURIComponent(callId);
  } catch {
    // Not valid percent-encoding: taken as written.
  }
  return callId !== 'query' ? callId : null;
};

/** The address of a meeting's page, for a link. */
export const meetingHref = (callId) => `#${CALLS_PATH}/${encodeURIComponent(callId)}`;
