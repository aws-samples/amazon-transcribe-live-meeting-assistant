/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * One troubleshooting agent diagnosis, from starting it to its answer.
 *
 * A diagnosis runs for up to about five minutes, longer than one AppSync request
 * may take, so the backend runs it as a job (see docs/troubleshooting-agent.md):
 * `start` records it and returns it PENDING, and the job is then read every
 * `pollIntervalMs` until it is COMPLETED or FAILED. Reading stops when the
 * component using the hook unmounts, and a job started later replaces the one
 * being read.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import { getTroubleshootingJob, startTroubleshooting } from '../graphql/queries/troubleshooting';

const logger = new ConsoleLogger('useTroubleshootingJob');

export const TERMINAL_STATUSES = ['COMPLETED', 'FAILED'];
export const DEFAULT_POLL_INTERVAL_MS = 3000;
/** Consecutive failed reads after which the diagnosis is shown as failed. */
export const MAX_FAILED_READS = 3;

// Built on first use rather than at import, so importing this module needs no
// configured Amplify.
let graphqlClient = null;
const client = () => {
  if (!graphqlClient) graphqlClient = generateClient();
  return graphqlClient;
};

export const isTerminal = (job) => Boolean(job && TERMINAL_STATUSES.includes(job.Status));

/** The message of a GraphQL failure, as Amplify reports it, or of a thrown Error. */
export const graphqlErrorMessage = (error) =>
  error?.errors?.[0]?.message || error?.message || 'The request could not be completed.';

const useTroubleshootingJob = ({ pollIntervalMs = DEFAULT_POLL_INTERVAL_MS } = {}) => {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [starting, setStarting] = useState(false);
  // Which start the state belongs to, so an answer to an earlier one is dropped.
  const generation = useRef(0);
  const failedReads = useRef(0);

  const start = useCallback(async ({ question = '', callId = '', vpId = '' } = {}) => {
    generation.current += 1;
    const mine = generation.current;
    failedReads.current = 0;
    setStarting(true);
    setError(null);
    setJob(null);
    const input = {};
    if (question.trim()) input.Question = question.trim();
    if (callId) input.CallId = callId;
    if (vpId) input.VirtualParticipantId = vpId;
    try {
      const result = await client().graphql({ query: startTroubleshooting, variables: { input } });
      if (mine === generation.current) setJob(result?.data?.startTroubleshooting || null);
    } catch (err) {
      logger.error('could not start a diagnosis', err);
      if (mine === generation.current) setError(graphqlErrorMessage(err));
    } finally {
      if (mine === generation.current) setStarting(false);
    }
  }, []);

  const reset = useCallback(() => {
    generation.current += 1;
    setJob(null);
    setError(null);
    setStarting(false);
  }, []);

  useEffect(() => {
    if (!job?.JobId || isTerminal(job)) return undefined;
    const mine = generation.current;
    const timer = setTimeout(async () => {
      try {
        const result = await client().graphql({ query: getTroubleshootingJob, variables: { JobId: job.JobId } });
        const next = result?.data?.getTroubleshootingJob;
        // A new object even when nothing changed, so the next read is scheduled.
        if (mine === generation.current && next) {
          failedReads.current = 0;
          setJob({ ...next });
        }
      } catch (err) {
        logger.error('could not read a diagnosis', err);
        if (mine !== generation.current) return;
        failedReads.current += 1;
        if (failedReads.current >= MAX_FAILED_READS) {
          setError(graphqlErrorMessage(err));
          setJob((previous) => (previous ? { ...previous, Status: 'FAILED' } : previous));
        } else {
          // Read again: one failed read is not a failed diagnosis.
          setJob((previous) => (previous ? { ...previous } : previous));
        }
      }
    }, pollIntervalMs);
    return () => clearTimeout(timer);
  }, [job, pollIntervalMs]);

  const running = starting || Boolean(job && !isTerminal(job));
  return { job, error, starting, running, start, reset };
};

export default useTroubleshootingJob;
