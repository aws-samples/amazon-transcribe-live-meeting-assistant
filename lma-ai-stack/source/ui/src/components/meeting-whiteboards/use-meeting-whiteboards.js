/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import listMeetingWhiteboards from '../../graphql/queries/listMeetingWhiteboards';
import getMeetingWhiteboard from '../../graphql/queries/getMeetingWhiteboard';
import listMeetingWhiteboardVersions from '../../graphql/queries/listMeetingWhiteboardVersions';
import createMeetingWhiteboard from '../../graphql/queries/createMeetingWhiteboard';
import updateMeetingWhiteboard from '../../graphql/queries/updateMeetingWhiteboard';
import deleteMeetingWhiteboard from '../../graphql/queries/deleteMeetingWhiteboard';
import requestWhiteboardUpdate from '../../graphql/queries/requestWhiteboardUpdate';
import updateCallWhiteboards from '../../graphql/queries/updateCallWhiteboards';
import onMeetingWhiteboard from '../../graphql/queries/onMeetingWhiteboard';
import { errorMessage, isConflict, mergeWhiteboard } from './whiteboard-model';

const logger = new ConsoleLogger('useMeetingWhiteboards');

const EMPTY = { items: [], whiteboardsEnabled: false, assistantAvailable: false, maxWhiteboards: 5, writable: true };

/**
 * A meeting's whiteboards, kept current by onMeetingWhiteboard, with what the
 * page can do to them. Each action resolves to { error, conflict } -- error a
 * message or null -- and merges the stored whiteboard it answers with, so the
 * page shows what was stored rather than what it sent. `client` is for tests.
 */
const useMeetingWhiteboards = (callId, { client: injected } = {}) => {
  const clientRef = useRef(injected || null);
  const getClient = () => {
    if (!clientRef.current) clientRef.current = generateClient();
    return clientRef.current;
  };
  const [state, setState] = useState(EMPTY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const latestCall = useRef(callId);
  latestCall.current = callId;

  const merge = useCallback((board) => {
    setState((previous) => ({ ...previous, items: mergeWhiteboard(previous.items, board) }));
  }, []);

  const load = useCallback(async () => {
    if (!callId) return;
    setLoading(true);
    try {
      const response = await getClient().graphql({ query: listMeetingWhiteboards, variables: { callId } });
      const answer = response?.data?.listMeetingWhiteboards || {};
      if (latestCall.current !== callId) return;
      setState({
        items: (answer.Items || []).reduce(mergeWhiteboard, []),
        whiteboardsEnabled: answer.WhiteboardsEnabled === true,
        assistantAvailable: answer.AssistantAvailable === true,
        maxWhiteboards: answer.MaxWhiteboards || EMPTY.maxWhiteboards,
        writable: answer.Writable !== false,
      });
      setError(null);
    } catch (err) {
      logger.error('could not read the meeting whiteboards', err);
      if (latestCall.current === callId) setError(errorMessage(err));
    } finally {
      if (latestCall.current === callId) setLoading(false);
    }
  }, [callId]);

  useEffect(() => {
    setState(EMPTY);
    setError(null);
    load();
  }, [load]);

  useEffect(() => {
    if (!callId) return undefined;
    let subscription;
    try {
      subscription = getClient()
        .graphql({ query: onMeetingWhiteboard, variables: { callId } })
        .subscribe({
          next: (message) => {
            const board = message?.data?.onMeetingWhiteboard;
            if (board && board.CallId === latestCall.current) merge(board);
          },
          error: (err) => logger.error('meeting whiteboard subscription failed', err),
        });
    } catch (err) {
      logger.error('could not subscribe to meeting whiteboard changes', err);
    }
    return () => subscription?.unsubscribe?.();
  }, [callId, merge]);

  const run = useCallback(
    async (query, name, input) => {
      try {
        const response = await getClient().graphql({ query, variables: { input: { CallId: callId, ...input } } });
        const board = response?.data?.[name];
        if (board) merge(board);
        return { error: null, conflict: false, board };
      } catch (err) {
        logger.error(`${name} failed`, err);
        return { error: errorMessage(err), conflict: isConflict(err) };
      }
    },
    [callId, merge],
  );

  const create = useCallback((input) => run(createMeetingWhiteboard, 'createMeetingWhiteboard', input), [run]);

  /** A change from the version the page holds; the board is the one being changed. */
  const update = useCallback(
    (board, change) =>
      run(updateMeetingWhiteboard, 'updateMeetingWhiteboard', {
        WhiteboardId: board.WhiteboardId,
        BaseVersion: board.Version,
        ...change,
      }),
    [run],
  );

  const remove = useCallback(
    (board) => run(deleteMeetingWhiteboard, 'deleteMeetingWhiteboard', { WhiteboardId: board.WhiteboardId }),
    [run],
  );

  /** One whiteboard read again, after a conflict. */
  const reloadBoard = useCallback(
    async (whiteboardId) => {
      try {
        const response = await getClient().graphql({
          query: getMeetingWhiteboard,
          variables: { callId, whiteboardId },
        });
        const board = response?.data?.getMeetingWhiteboard;
        // The newer copy replaces the held one whatever its version.
        if (board) {
          setState((previous) => ({
            ...previous,
            items: mergeWhiteboard(
              previous.items.filter((b) => b.WhiteboardId !== whiteboardId),
              board,
            ),
          }));
        }
        return null;
      } catch (err) {
        logger.error('could not read the whiteboard again', err);
        return errorMessage(err);
      }
    },
    [callId],
  );

  const readVersion = useCallback(
    async (whiteboardId, version) => {
      const response = await getClient().graphql({
        query: getMeetingWhiteboard,
        variables: { callId, whiteboardId, version },
      });
      return response?.data?.getMeetingWhiteboard || null;
    },
    [callId],
  );

  const listVersions = useCallback(
    async (whiteboardId, nextToken) => {
      const response = await getClient().graphql({
        query: listMeetingWhiteboardVersions,
        variables: { callId, whiteboardId, limit: 50, ...(nextToken ? { nextToken } : {}) },
      });
      const page = response?.data?.listMeetingWhiteboardVersions || {};
      return { items: page.Items || [], nextToken: page.NextToken || null };
    },
    [callId],
  );

  /** The meeting's own switch. Its answer reaches the page through onUpdateCall too. */
  const setAssistantEnabled = useCallback(
    async (enabled) => {
      try {
        await getClient().graphql({
          query: updateCallWhiteboards,
          variables: { input: { CallId: callId, WhiteboardsEnabled: enabled } },
        });
        setState((previous) => ({ ...previous, whiteboardsEnabled: enabled }));
        return null;
      } catch (err) {
        logger.error('could not change the whiteboards switch', err);
        return errorMessage(err);
      }
    },
    [callId],
  );

  /** One run now. Resolves to { error, message }. */
  const requestUpdate = useCallback(async () => {
    try {
      const response = await getClient().graphql({ query: requestWhiteboardUpdate, variables: { callId } });
      const answer = response?.data?.requestWhiteboardUpdate || {};
      return {
        error: answer.Success === false ? answer.Message || 'Nothing was updated.' : null,
        message: answer.Message,
      };
    } catch (err) {
      logger.error('could not update the whiteboards now', err);
      return { error: errorMessage(err), message: null };
    }
  }, [callId]);

  return {
    ...state,
    loading,
    error,
    reload: load,
    reloadBoard,
    create,
    update,
    remove,
    readVersion,
    listVersions,
    setAssistantEnabled,
    requestUpdate,
  };
};

export default useMeetingWhiteboards;
