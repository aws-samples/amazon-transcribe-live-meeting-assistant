/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting's estimated cost, read through getCallCost (see
 * docs/meeting-cost-estimate.md). One read serves every place the page shows
 * the estimate -- the header's cost chip and the Details tab -- so they cannot
 * disagree, and the meeting is not priced twice per refresh.
 *
 * Loaded when the meeting changes and whenever `refreshKey` does. With no
 * `callId` it does nothing, so a component can hold the hook and use an
 * estimate passed in instead.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getCallCost from '../graphql/queries/getCallCost';

const logger = new ConsoleLogger('useCallCost');

export const defaultLoadCost = async (callId) => {
  const client = generateClient();
  const response = await client.graphql({ query: getCallCost, variables: { CallId: callId } });
  return response?.data?.getCallCost || null;
};

const useCallCost = (callId, refreshKey = '', loadCost = defaultLoadCost) => {
  const [estimate, setEstimate] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Only the latest request may update the estimate: a refresh pressed while a
  // reload is in flight, or a different meeting opened mid-request, must not be
  // overwritten by the older answer arriving last.
  const latestRequest = useRef(0);

  const refresh = useCallback(async () => {
    if (!callId) return;
    latestRequest.current += 1;
    const request = latestRequest.current;
    setLoading(true);
    setError(null);
    try {
      const result = await loadCost(callId);
      if (request === latestRequest.current) setEstimate(result);
    } catch (err) {
      logger.error('could not load the cost estimate', err);
      if (request === latestRequest.current) {
        // Cleared, so a failed refresh is not shown above an older table as if
        // that table were current.
        setEstimate(null);
        setError(err?.errors?.[0]?.message || err?.message || 'The estimate could not be loaded.');
      }
    } finally {
      if (request === latestRequest.current) setLoading(false);
    }
  }, [callId, loadCost]);

  // The page keys refreshKey on the meeting's status and on its summary,
  // because most of a meeting's usage is recorded as it ends and the summary's
  // own usage is written just before the summary appears. The Virtual
  // Participant's runtime is written as it leaves, which can be after both;
  // the refresh button picks that up.
  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  return { estimate, loading, error, refresh };
};

export default useCallCost;
