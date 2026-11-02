/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getMeetingConsent from '../graphql/queries/getMeetingConsent';
import onUpdateMeetingConsent from '../graphql/queries/onUpdateMeetingConsent';
import recordMeetingConsentEvent from '../graphql/queries/recordMeetingConsentEvent';
import attestMeetingConsent from '../graphql/queries/attestMeetingConsent';
import verifyMeetingConsentEvidence from '../graphql/queries/verifyMeetingConsentEvidence';

const logger = new ConsoleLogger('useMeetingConsent');

/** How long after a change is announced the record is read, so a burst is read once. */
export const READ_DELAY_MS = 750;

/** The first GraphQL error's message, or the error's own. */
export const errorMessage = (error) =>
  error?.errors?.[0]?.message || error?.message || 'the request could not be completed';

/**
 * A meeting's consent record, kept current, with the three things the meeting
 * page can do to it.
 *
 * Reads getMeetingConsent, which signs the evidence links, and reads it again
 * whenever onUpdateMeetingConsent announces a change, whoever made it. Each
 * action reads it again too, so the page shows the stored record rather than
 * what it sent. `client` is for tests.
 */
const useMeetingConsent = (callId, { client: injected } = {}) => {
  const clientRef = useRef(injected || null);
  const getClient = () => {
    if (!clientRef.current) clientRef.current = generateClient();
    return clientRef.current;
  };
  const [state, setState] = useState({ consent: null, captureAllowed: true, settings: null });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const latestCall = useRef(callId);
  latestCall.current = callId;
  const heldVersion = useRef(null);
  const pending = useRef(null);
  // Mutations from this page that have not been read back yet; their own
  // subscription messages need no second read.
  const inFlight = useRef(0);

  const load = useCallback(async () => {
    if (!callId) return;
    setLoading(true);
    try {
      const response = await getClient().graphql({ query: getMeetingConsent, variables: { callId } });
      const answer = response?.data?.getMeetingConsent;
      // A slow answer for a meeting the page has moved away from is dropped.
      if (latestCall.current !== callId) return;
      heldVersion.current = answer?.Consent?.Version ?? null;
      setState({
        consent: answer?.Consent || null,
        captureAllowed: answer?.CaptureAllowed ?? true,
        settings: answer?.Settings || null,
      });
      setError(null);
    } catch (err) {
      logger.error('could not read the meeting consent record', err);
      if (latestCall.current === callId) setError(errorMessage(err));
    } finally {
      if (latestCall.current === callId) setLoading(false);
    }
  }, [callId]);

  useEffect(() => {
    setState({ consent: null, captureAllowed: true, settings: null });
    setError(null);
    heldVersion.current = null;
    load();
  }, [load]);

  useEffect(() => {
    if (!callId) return undefined;
    let subscription;
    try {
      subscription = getClient()
        .graphql({ query: onUpdateMeetingConsent, variables: { callId } })
        .subscribe({
          // A capture client writes often (each change of who is present), and
          // every read signs the images again; so a message for the version
          // already held is skipped and a burst is read once.
          next: (message) => {
            const version = message?.data?.onUpdateMeetingConsent?.Consent?.Version;
            if (inFlight.current > 0) return;
            if (version != null && version === heldVersion.current) return;
            clearTimeout(pending.current);
            pending.current = setTimeout(load, READ_DELAY_MS);
          },
          error: (err) => logger.error('meeting consent subscription failed', err),
        });
    } catch (err) {
      logger.error('could not subscribe to meeting consent changes', err);
    }
    return () => {
      clearTimeout(pending.current);
      subscription?.unsubscribe?.();
    };
  }, [callId, load]);

  /** Run one mutation, then read the record again. Resolves to an error message or null. */
  const mutate = useCallback(
    async (query, input) => {
      inFlight.current += 1;
      try {
        await getClient().graphql({ query, variables: { input: { CallId: callId, ...input } } });
        await load();
        return null;
      } catch (err) {
        logger.error('meeting consent change failed', err);
        return errorMessage(err);
      } finally {
        inFlight.current -= 1;
      }
    },
    [callId, load],
  );

  const recordObjection = useCallback(
    (participantName, text) =>
      mutate(recordMeetingConsentEvent, {
        ParticipantName: participantName,
        Decision: 'objection',
        ...(text ? { Text: text } : {}),
      }),
    [mutate],
  );
  const attest = useCallback(
    (method, statement) =>
      mutate(attestMeetingConsent, { Method: method, ...(statement ? { Statement: statement } : {}) }),
    [mutate],
  );
  const verifyEvidence = useCallback(
    (evidenceId) => mutate(verifyMeetingConsentEvidence, { EvidenceId: evidenceId }),
    [mutate],
  );

  return { ...state, loading, error, reload: load, recordObjection, attest, verifyEvidence };
};

export default useMeetingConsent;
