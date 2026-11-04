/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef } from 'react';
import * as ReactUseWebSocket from 'react-use-websocket';
import { fetchAuthSession } from 'aws-amplify/auth';
import { ConsoleLogger } from 'aws-amplify/utils';

import { createTranscriberStream } from '../components/stream-audio/transcriber-stream';

// react-use-websocket ships a CJS bundle. Vite 8 / rolldown exports the CJS
// module.exports OBJECT as the ESM default (a "double-wrapped default"), so the
// hook is not the default itself but one level in. Unwrap to whichever level is
// actually the function, so this works whichever interop the bundler applies.
// Getting it wrong leaves useWebSocket undefined, and calling it throws during
// render, which blanks the page. This hook is the only place the library is
// imported.
const useWebSocket =
  typeof ReactUseWebSocket.default === 'function' ? ReactUseWebSocket.default : ReactUseWebSocket.default.default;

const logger = new ConsoleLogger('useTranscriberSocket');

/**
 * The transcriber's upgrade URL. A browser WebSocket cannot set headers, so the
 * tokens go in the query string. The refresh token is not available from
 * Amplify v6 and the transcriber does not need it.
 */
export const transcriberUrl = (endpoint, accessToken, idToken) => {
  const query =
    `authorization=${encodeURIComponent(`Bearer ${accessToken}`)}` +
    `&id_token=${encodeURIComponent(idToken)}&refresh_token=`;
  return `${endpoint}${endpoint.includes('?') ? '&' : '?'}${query}`;
};

/** 1 s, 2 s, 4 s, then every 5 s: soon enough that little audio is lost to a short drop. */
export const reconnectInterval = (attempt) => Math.min(1000 * 2 ** attempt, 5000);

/**
 * The page's connection to the WebSocket transcriber, and the meeting streamed
 * over it (see transcriber-stream.js). Reconnects on any close, with tokens
 * fetched for each connection, so a reconnect after the tokens held by the app
 * have expired still authenticates. Tokens are not passed as the hook's query
 * parameters: a change in those makes react-use-websocket close and reopen the
 * socket, which the app's periodic session refresh would then do mid-meeting.
 *
 * `session` (the app context's session) is used only if fetching one fails.
 * `onOpen` runs after the stream has sent what it needs on the new connection.
 */
const useTranscriberSocket = ({ endpoint, session, onOpen, onMessage, onResumeExpired }) => {
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const callbacks = useRef({});
  callbacks.current = { onOpen, onMessage, onResumeExpired };

  const streamRef = useRef(null);
  if (streamRef.current === null) {
    streamRef.current = createTranscriberStream({
      onResumeExpired: () => callbacks.current.onResumeExpired?.(),
      log: (message) => logger.info(message),
    });
  }
  const stream = streamRef.current;
  // Clears the resume timer when the page goes away.
  useEffect(() => () => stream.abandon(), [stream]);

  const getSocketUrl = useCallback(async () => {
    let tokens;
    try {
      // Amplify refreshes expired tokens here.
      tokens = (await fetchAuthSession()).tokens;
    } catch (error) {
      logger.warn('Could not fetch a session for the transcriber connection; using the last one', error);
    }
    tokens = tokens || sessionRef.current?.tokens;
    return transcriberUrl(endpoint, tokens?.accessToken?.toString() ?? '', tokens?.idToken?.toString() ?? '');
  }, [endpoint]);

  useWebSocket(endpoint ? getSocketUrl : null, {
    onOpen: (event) => {
      logger.debug('Transcriber connection open');
      stream.opened(event.target);
      callbacks.current.onOpen?.(event);
    },
    onMessage: (event) => callbacks.current.onMessage?.(event),
    onClose: (event) => {
      logger.debug(`Transcriber connection closed (${event?.code})`);
      stream.closed(event.target);
    },
    onError: (event) => logger.debug('Transcriber connection error', event),
    shouldReconnect: () => true,
    reconnectInterval,
  });

  return stream;
};

export default useTranscriberSocket;
