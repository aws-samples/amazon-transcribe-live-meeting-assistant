/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import { useCallback, useEffect, useRef, useState } from 'react';
import useAppContext from '../contexts/app';

import listCallsDateRange from '../graphql/queries/listCallsDateRange';
import getCallCount from '../graphql/queries/getCallCount';
import getCall from '../graphql/queries/getCall';

import onCreateCall from '../graphql/queries/onCreateCall';
import onAddTranscriptSegment from '../graphql/queries/onAddTranscriptSegment';
import onUpdateCall from '../graphql/queries/onUpdateCall';
import onShareMeetings from '../graphql/queries/onShareMeetings';
import onDeleteCall from '../graphql/queries/onDeleteCall';
import onUnshareCall from '../graphql/queries/onUnshareCall';
import getTranscriptSegments from '../graphql/queries/getTranscriptSegments';
import updateSpeakerIdentities from '../graphql/queries/updateSpeakerIdentities';
import resolveSpeakerNames from '../graphql/queries/resolveSpeakerNames';
import updateCallSummaryText from '../graphql/queries/updateCallSummaryText';
import regenerateCallSummary from '../graphql/queries/regenerateCallSummary';
import updateCallAgenda from '../graphql/queries/updateCallAgenda';

import { DEFAULT_DATE_RANGE_HOURS, hoursToDateRange } from '../components/call-list/calls-table-config';
import { serializeSummaryState } from '../components/common/summary-state';

const client = generateClient();
const logger = new ConsoleLogger('useCallsGraphQlApi');

// Matches the `limit` used server-side by the ListCallsGsiResolver.  One
// round-trip of 100 meeting rows covers the typical preset windows; we
// then paginate via nextToken in the background until the server returns
// a null token or MAX_PAGES_PER_LOAD is hit.
const LIST_PAGE_SIZE = 100;
const MAX_PAGES_PER_LOAD = 20;

const useCallsGraphQlApi = ({ initialDateRange } = {}) => {
  // dateRange is { startDateIso, endDateIso } — the active meeting-list window.
  const [dateRange, setDateRange] = useState(initialDateRange || hoursToDateRange(DEFAULT_DATE_RANGE_HOURS));
  // Mirror of dateRange held in a ref so subscription callbacks (which close
  // over an old dateRange otherwise) always see the latest value.
  const dateRangeRef = useRef(dateRange);
  useEffect(() => {
    dateRangeRef.current = dateRange;
  }, [dateRange]);

  const [isCallsListLoading, setIsCallsListLoading] = useState(false);
  const [calls, setCalls] = useState([]);
  const [liveTranscriptCallId, setLiveTranscriptCallId] = useState();
  const [callTranscriptPerCallId, setCallTranscriptPerCallId] = useState({});
  // Server-side total matching the active dateRange + caller's RBAC scope.
  // `null` = not yet loaded; number = known count.  Used by the UI to
  // display e.g. "5 loaded of 27 total" in the list header.
  const [totalCallCount, setTotalCallCount] = useState(null);
  const [totalCallCountTruncated, setTotalCallCountTruncated] = useState(false);
  const { setErrorMessage } = useAppContext();

  /**
   * Returns true iff a call (identified by CreatedAt) falls within the
   * currently active dateRange.  Used by subscription handlers to drop
   * out-of-range events so the UI list stays consistent.
   */
  const isWithinActiveRange = (createdAtIso) => {
    if (!createdAtIso) return true; // permissive on missing data
    const { startDateIso, endDateIso } = dateRangeRef.current || {};
    if (!startDateIso || !endDateIso) return true;
    const t = new Date(createdAtIso).getTime();
    return t >= new Date(startDateIso).getTime() && t <= new Date(endDateIso).getTime();
  };

  /**
   * Replace calls in state with freshly fetched ones.
   *
   * An incoming call object REPLACES the stored one, so a field that the query or
   * subscription which produced it did not select would be dropped from state.
   * The fields listed here are carried over from the existing object for that
   * reason: ListPK/ListSK come only from the list query, and SpeakerIdentities is
   * deliberately left out of the list query (it would ship a name map for every
   * meeting in the range when only the open one needs it), so a list refresh
   * would otherwise blank the names on the meeting being viewed.
   *
   * CallSummaryState is carried over for a different reason: the Kinesis
   * pipeline's Call mutations are published to onUpdateCall too, and they select
   * their own fixed field list, so a status or aggregation update arriving
   * during a live meeting would otherwise blank it -- making a hand-edited
   * summary read as generated and hiding a regeneration in flight.
   *
   * The agenda is carried over for the same reason, as one unit keyed on
   * AgendaUpdatedAt: every agenda write sets that timestamp, so an incoming call
   * without one came from a mutation that did not select the agenda, while one
   * with it is authoritative for all four fields -- including an agenda that
   * was cleared, which a per-field `||` would resurrect.
   */
  const setCallsDeduped = (callValues) => {
    setCalls((currentCalls) => {
      const callValuesCallIds = callValues.map((c) => c.CallId);
      return [
        ...currentCalls.filter((c) => !callValuesCallIds.includes(c.CallId)),
        ...callValues.map((call) => {
          const existing = currentCalls.find((c) => c.CallId === call.CallId);
          return {
            ...call,
            ListPK: call.ListPK || existing?.ListPK,
            ListSK: call.ListSK || existing?.ListSK,
            SpeakerIdentities: call.SpeakerIdentities || existing?.SpeakerIdentities,
            CallSummaryState: call.CallSummaryState || existing?.CallSummaryState,
            ...(call.AgendaUpdatedAt
              ? {}
              : {
                  Agenda: existing?.Agenda,
                  AgendaSource: existing?.AgendaSource,
                  AgendaUpdatedBy: existing?.AgendaUpdatedBy,
                  AgendaUpdatedAt: existing?.AgendaUpdatedAt,
                }),
          };
        }),
      ];
    });
  };

  const getCallDetailsFromCallIds = async (callIds) => {
    const getCallPromises = callIds.map((callId) => client.graphql({ query: getCall, variables: { callId } }));
    const getCallResolutions = await Promise.allSettled(getCallPromises);
    const getCallRejected = getCallResolutions.filter((r) => r.status === 'rejected');
    if (getCallRejected.length) {
      setErrorMessage('failed to get call details - please try again later');
      logger.error('get call promises rejected', getCallRejected);
    }
    return getCallResolutions.filter((r) => r.status === 'fulfilled').map((r) => r.value?.data?.getCall);
  };

  useEffect(() => {
    logger.debug('onCreateCall subscription');
    const subscription = client.graphql({ query: onCreateCall }).subscribe({
      next: async (message) => {
        logger.debug('call list subscription update', message);
        const callId = message.data?.onCreateCall?.CallId || '';
        if (!callId) return;
        const callValues = await getCallDetailsFromCallIds([callId]);
        // Filter out calls whose CreatedAt falls outside the currently
        // active date range (stale-closure safe via useRef).
        const inRange = callValues.filter((c) => isWithinActiveRange(c?.CreatedAt));
        if (inRange.length) setCallsDeduped(inRange);
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('call list network subscription failed - please reload the page');
      },
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    logger.debug('onUpdateCall subscription');
    const subscription = client.graphql({ query: onUpdateCall }).subscribe({
      next: async (message) => {
        logger.debug('call update', message);
        const callUpdateEvent = message.data?.onUpdateCall;
        if (!callUpdateEvent?.CallId) return;
        if (!isWithinActiveRange(callUpdateEvent?.CreatedAt)) return;
        setCallsDeduped([callUpdateEvent]);
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('call update network request failed - please reload the page');
      },
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    logger.debug('onShareMeetings subscription');
    const subscription = client.graphql({ query: onShareMeetings }).subscribe({
      next: async (message) => {
        logger.debug('share meetings subscription update', message);
        const sharedCalls = message.data?.onShareMeetings?.Calls || '';
        if (sharedCalls && sharedCalls.length > 0) {
          const callValues = await getCallDetailsFromCallIds(sharedCalls);
          const inRange = callValues.filter((c) => isWithinActiveRange(c?.CreatedAt));
          if (inRange.length) setCallsDeduped(inRange);
        }
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('share meetings subscription failed - please reload the page');
      },
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    logger.debug('onDeleteCall subscription');
    const subscription = client.graphql({ query: onDeleteCall }).subscribe({
      next: async (message) => {
        logger.debug('call delete subscription update', message);
        const callId = message.data?.onDeleteCall?.CallId || '';
        if (callId) {
          setCalls((currentCalls) => currentCalls.filter((c) => c.CallId !== callId));
        }
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('call delete subscription failed - please reload the page');
      },
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    logger.debug('onUnshareCall subscription');
    const subscription = client.graphql({ query: onUnshareCall }).subscribe({
      next: async (message) => {
        logger.debug('call unshare subscription update', message);
        const callId = message.data?.onUnshareCall?.CallId || '';
        if (callId) {
          setCalls((currentCalls) => currentCalls.filter((c) => c.CallId !== callId));
        }
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('call delete subscription failed - please reload the page');
      },
    });
    return () => subscription.unsubscribe();
  }, []);

  const handleCallTranscriptSegmentMessage = (transcriptSegment) => {
    const { callId, transcript, isPartial, channel } = transcriptSegment;

    setCallTranscriptPerCallId((current) => {
      const currentContactEntry = current[callId] || {};
      const currentChannelEntry = currentContactEntry[channel] || {};
      const currentBase = currentChannelEntry?.base || '';
      const currentSegments = currentChannelEntry?.segments || [];
      const lastSameSegmentId = currentSegments.filter((s) => s.segmentId === transcriptSegment.segmentId).pop();
      const dedupedSegments = currentSegments.filter((s) => s.segmentId !== transcriptSegment.segmentId);

      const segments = [
        ...dedupedSegments,
        // prettier-ignore
        (lastSameSegmentId?.isPartial === false && transcriptSegment?.isPartial === true)
        || (lastSameSegmentId?.isPartial === false && lastSameSegmentId?.sentiment)
          ? lastSameSegmentId
          : transcriptSegment,
      ].sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt));

      const entry = {
        ...currentContactEntry,
        [channel]: {
          base: !isPartial ? `${currentBase} ${transcript}`.trim() : currentBase,
          lastPartial: isPartial ? transcript : '',
          segments,
        },
      };
      return { ...current, [callId]: { ...entry } };
    });
  };

  const mapTranscriptSegmentValue = (transcriptSegmentValue) => {
    const {
      CallId: callId,
      SegmentId: segmentId,
      StartTime: startTime,
      EndTime: endTime,
      Speaker: speaker,
      Transcript: transcript,
      IsPartial: isPartial,
      Channel: channel,
      CreatedAt: createdAt,
      Sentiment: sentiment,
      SentimentScore: sentimentScore,
      SentimentWeighted: sentimentWeighted,
    } = transcriptSegmentValue;

    return {
      callId,
      segmentId,
      startTime,
      endTime,
      speaker,
      transcript,
      isPartial,
      channel,
      createdAt,
      sentiment,
      sentimentScore,
      sentimentWeighted,
    };
  };

  useEffect(() => {
    let subscription;
    logger.debug('onAddTranscriptSegment effect');

    if (!liveTranscriptCallId) {
      if (subscription?.unsubscribe) {
        subscription.unsubscribe();
      }
      return () => {};
    }

    subscription = client
      .graphql({ query: onAddTranscriptSegment, variables: { callId: liveTranscriptCallId } })
      .subscribe({
        next: async (message) => {
          const transcriptSegmentValue = message.data?.onAddTranscriptSegment;
          if (!transcriptSegmentValue) return;
          const transcriptSegment = mapTranscriptSegmentValue(transcriptSegmentValue);
          const { callId, transcript, segmentId } = transcriptSegment;
          if (callId !== liveTranscriptCallId) return;
          if (transcript && segmentId) {
            handleCallTranscriptSegmentMessage(transcriptSegment);
          }
        },
        error: (error) => {
          logger.error(error);
          setErrorMessage('transcript update network subscription failed - please reload the page');
        },
      });

    return () => subscription.unsubscribe();
  }, [liveTranscriptCallId]);

  /**
   * Load all calls whose CreatedAt falls within the active dateRange by
   * calling listCallsDateRange and following the server-provided nextToken
   * until exhausted or we hit the MAX_PAGES_PER_LOAD safety limit.
   *
   * Calls returned by the GSI resolver are already fully hydrated (the
   * resolver BatchGetItems the `c#<CallId>` detail rows internally) so we
   * don't need to follow up with per-call getCall requests.
   */
  /**
   * Load the total server-side count of calls matching the active range
   * (RBAC-filtered server-side).  Runs in parallel with loadCallsForDateRange
   * so the header can show the total even before the full list is paginated.
   */
  const loadCallCountForDateRange = async (activeRange) => {
    const { startDateIso, endDateIso } = activeRange || {};
    if (!startDateIso || !endDateIso) return;
    try {
      const response = await client.graphql({
        query: getCallCount,
        variables: { startDateTime: startDateIso, endDateTime: endDateIso },
      });
      // Abort if the user changed the range while we were waiting
      if (
        dateRangeRef.current !== activeRange &&
        JSON.stringify(dateRangeRef.current) !== JSON.stringify(activeRange)
      ) {
        return;
      }
      const count = response?.data?.getCallCount?.count;
      const truncated = !!response?.data?.getCallCount?.truncated;
      setTotalCallCount(typeof count === 'number' ? count : null);
      setTotalCallCountTruncated(truncated);
    } catch (error) {
      // Non-fatal — just leave the counter unset.
      logger.warn('getCallCount failed (header counter will be hidden)', error);
      setTotalCallCount(null);
      setTotalCallCountTruncated(false);
    }
  };

  const loadCallsForDateRange = async () => {
    const { startDateIso, endDateIso } = dateRange || {};
    if (!startDateIso || !endDateIso) {
      setIsCallsListLoading(false);
      return;
    }
    try {
      setCalls([]);
      setTotalCallCount(null);
      setTotalCallCountTruncated(false);
      let nextToken = null;
      let pages = 0;
      const collected = [];
      // Snapshot the range we started this load with. If the user changes
      // the range while we're still paginating, stop early so we don't
      // populate the list with calls from a stale window.
      const activeRange = dateRange;
      // Fire-and-forget the count query in parallel with the list query.
      loadCallCountForDateRange(activeRange);
      do {
        // eslint-disable-next-line no-await-in-loop
        const response = await client.graphql({
          query: listCallsDateRange,
          variables: {
            startDateTime: activeRange.startDateIso,
            endDateTime: activeRange.endDateIso,
            limit: LIST_PAGE_SIZE,
            nextToken,
          },
        });
        const page = response?.data?.listCallsDateRange?.Calls || [];
        // Early-out if the range changed under us.
        if (
          dateRangeRef.current !== activeRange &&
          JSON.stringify(dateRangeRef.current) !== JSON.stringify(activeRange)
        ) {
          logger.debug('date range changed during pagination — aborting');
          return;
        }
        page.forEach((c) => collected.push({ ...c, ListPK: c.PK, ListSK: c.SK }));
        setCallsDeduped(page.map((c) => ({ ...c, ListPK: c.PK, ListSK: c.SK })));
        nextToken = response?.data?.listCallsDateRange?.nextToken || null;
        pages += 1;
      } while (nextToken && pages < MAX_PAGES_PER_LOAD);
      logger.debug(`loaded ${collected.length} calls across ${pages} page(s)`);
    } catch (error) {
      setErrorMessage('failed to list calls - please try again later');
      logger.error('loadCallsForDateRange failed', error);
    } finally {
      setIsCallsListLoading(false);
    }
  };

  useEffect(() => {
    if (isCallsListLoading) {
      // defer so we don't block rendering
      setTimeout(() => {
        loadCallsForDateRange();
      }, 1);
    }
  }, [isCallsListLoading]);

  useEffect(() => {
    logger.debug('date range changed', dateRange);
    setIsCallsListLoading(true);
  }, [dateRange]);

  const sendGetTranscriptSegmentsRequest = async (callId) => {
    try {
      const response = await client.graphql({ query: getTranscriptSegments, variables: { callId } });
      const transcriptSegments = response?.data?.getTranscriptSegments?.TranscriptSegments;
      if (transcriptSegments?.length > 0) {
        const transcriptSegmentsReduced = transcriptSegments
          .map((t) => mapTranscriptSegmentValue(t))
          .reduce((p, c) => {
            const previousSegments = p[c.channel]?.segments || [];
            const lastSameSegmentId = previousSegments.filter((s) => s?.segmentId === c?.segmentId).pop();
            const dedupedSegments = previousSegments.filter((s) => s.segmentId !== c.segmentId);
            const segment = !lastSameSegmentId?.sentiment && c?.sentiment ? c : lastSameSegmentId || c;
            return { ...p, [c.channel]: { segments: [...dedupedSegments, segment] } };
          }, {});

        setCallTranscriptPerCallId((current) => ({
          ...current,
          [callId]: transcriptSegmentsReduced,
        }));
      }
    } catch (error) {
      setErrorMessage('failed to get transcript - please try again later');
      logger.error('failed to set transcript segments', error);
    }
  };

  /**
   * Write a meeting's speaker identity map.
   *
   * State is updated optimistically so the transcript re-labels on the keystroke
   * that confirmed the name rather than on the subscription round trip. Other
   * clients get the same change pushed through onUpdateCall. A rejected write is
   * rolled back, because leaving a name on screen that was never stored means the
   * next reload silently loses it.
   */
  const sendUpdateSpeakerIdentities = async (callId, serializedIdentities) => {
    // Captured from inside the updater, not from `calls` in the render closure:
    // renaming two voices in quick succession (blurring one field straight into
    // the next) runs both calls against the same stale `calls`, so a closure read
    // would roll the second failure back to the state before the FIRST rename and
    // drop a name that was in fact stored.
    let previous;
    // Patch in place rather than via setCallsDeduped: that helper REPLACES the
    // stored call with the object handed to it, so a two-field payload would
    // strip the rest of the meeting's attributes out of state.
    setCalls((currentCalls) => {
      previous = currentCalls.find((c) => c.CallId === callId)?.SpeakerIdentities;
      return currentCalls.map((c) => (c.CallId === callId ? { ...c, SpeakerIdentities: serializedIdentities } : c));
    });
    try {
      await client.graphql({
        query: updateSpeakerIdentities,
        variables: { input: { CallId: callId, SpeakerIdentities: serializedIdentities } },
      });
      return true;
    } catch (error) {
      logger.error('failed to update speaker identities', error);
      const refused = JSON.stringify(error?.errors ?? error ?? '').includes('condition failure');
      setErrorMessage(
        refused
          ? 'you do not have permission to change speaker names on this meeting'
          : 'failed to save speaker names - please try again later',
      );
      // Roll back only if what is on screen is still what THIS call put there.
      // Two renames can be in flight at once (blurring one field straight into
      // the next), and a blind revert would discard a later write that succeeded.
      setCalls((currentCalls) =>
        currentCalls.map((c) =>
          c.CallId === callId && c.SpeakerIdentities === serializedIdentities
            ? { ...c, SpeakerIdentities: previous }
            : c,
        ),
      );
      return false;
    }
  };

  /**
   * Ask the backend to infer speaker names for a meeting.
   *
   * Nothing is patched into state here: the resolver writes the names through
   * updateSpeakerIdentities, so they arrive via onUpdateCall like any other
   * rename and the transcript re-labels itself. Returns the backend's own count
   * so the caller can say what happened.
   */
  // Memoised, unlike its neighbours in this hook, because the Speakers panel
  // uses it as the dependency of an interval. A fresh identity each render would
  // tear down and rebuild that interval on every arriving transcript segment, so
  // it would only ever elapse during silence -- which is exactly when there is
  // nothing new to read.
  const sendResolveSpeakerNames = useCallback(async (callId, { force = false } = {}) => {
    try {
      const response = await client.graphql({
        query: resolveSpeakerNames,
        variables: { input: { CallId: callId, Force: force } },
      });
      return response?.data?.resolveSpeakerNames ?? { Success: false, NamesApplied: 0 };
    } catch (error) {
      logger.error('failed to resolve speaker names', error);
      // Deliberately not surfaced as a global error for an automatic pass: it
      // runs on a timer and a transient failure is not something the user asked
      // for or can act on. The on-demand caller reports it itself.
      return { Success: false, Message: 'request failed', NamesApplied: 0 };
    }
  }, []);

  /**
   * Store a meeting's summary text, and record that a person wrote it.
   *
   * State is updated optimistically so the edited text appears on the keystroke
   * that saved it rather than on the subscription round trip. Other clients get
   * the same change pushed through onUpdateCall. A rejected write is rolled
   * back, because leaving text on screen that was never stored means the next
   * reload silently loses it.
   */
  const sendUpdateCallSummary = async (callId, text, state) => {
    const serializedState = serializeSummaryState(state);
    // Captured from inside the updater, not from `calls` in the render closure,
    // for the same reason the speaker identity write does it: two saves can be
    // in flight at once, and a closure read would roll the second failure back
    // past the first success.
    let previousText;
    let previousState;
    setCalls((currentCalls) => {
      const existing = currentCalls.find((c) => c.CallId === callId);
      previousText = existing?.CallSummaryText;
      previousState = existing?.CallSummaryState;
      return currentCalls.map((c) =>
        c.CallId === callId ? { ...c, CallSummaryText: text, CallSummaryState: serializedState } : c,
      );
    });
    try {
      await client.graphql({
        query: updateCallSummaryText,
        variables: { input: { CallId: callId, CallSummaryText: text, CallSummaryState: serializedState } },
      });
      return true;
    } catch (error) {
      logger.error('failed to update the meeting summary', error);
      const refused = JSON.stringify(error?.errors ?? error ?? '').includes('condition failure');
      setErrorMessage(
        refused
          ? 'you do not have permission to edit the summary of this meeting'
          : 'failed to save the summary - please try again later',
      );
      // Roll back only if what is on screen is still what THIS call put there.
      setCalls((currentCalls) =>
        currentCalls.map((c) =>
          c.CallId === callId && c.CallSummaryText === text
            ? { ...c, CallSummaryText: previousText, CallSummaryState: previousState }
            : c,
        ),
      );
      return false;
    }
  };

  /**
   * Store a meeting's agenda as typed on the meeting page.
   *
   * Optimistic, with a rollback on a rejected write, like the summary and
   * speaker-name writers above. The resolver records the source as 'ui' and the
   * caller's own username whatever the browser sends, so the patch here only
   * anticipates what the subscription will confirm.
   */
  const sendUpdateCallAgenda = async (callId, agenda) => {
    const AGENDA_FIELDS = ['Agenda', 'AgendaSource', 'AgendaUpdatedBy', 'AgendaUpdatedAt'];
    let previous;
    setCalls((currentCalls) => {
      const existing = currentCalls.find((c) => c.CallId === callId);
      previous = Object.fromEntries(AGENDA_FIELDS.map((field) => [field, existing?.[field]]));
      return currentCalls.map((c) =>
        c.CallId === callId
          ? { ...c, Agenda: agenda, AgendaSource: 'ui', AgendaUpdatedAt: new Date().toISOString() }
          : c,
      );
    });
    try {
      await client.graphql({
        query: updateCallAgenda,
        variables: { input: { CallId: callId, Agenda: agenda } },
      });
      return true;
    } catch (error) {
      logger.error('failed to update the meeting agenda', error);
      const refused = JSON.stringify(error?.errors ?? error ?? '').includes('condition failure');
      setErrorMessage(
        refused
          ? 'you do not have permission to edit the agenda of this meeting'
          : 'failed to save the agenda - please try again later',
      );
      // Roll back only if what is on screen is still what THIS call put there.
      setCalls((currentCalls) =>
        currentCalls.map((c) => (c.CallId === callId && c.Agenda === agenda ? { ...c, ...previous } : c)),
      );
      return false;
    }
  };

  /**
   * Ask the backend to summarise a meeting again.
   *
   * Nothing is patched into state here: the run outlives the request, and the
   * new summary arrives through onUpdateCall like an end-of-meeting one does.
   * Returns the backend's own answer so the caller can say what happened.
   */
  const sendRegenerateCallSummary = useCallback(async (callId) => {
    try {
      const response = await client.graphql({
        query: regenerateCallSummary,
        variables: { input: { CallId: callId } },
      });
      return response?.data?.regenerateCallSummary ?? { Success: false, Message: 'no answer from the server' };
    } catch (error) {
      logger.error('failed to start regenerating the summary', error);
      return { Success: false, Message: 'request failed' };
    }
  }, []);

  return {
    calls,
    callTranscriptPerCallId,
    isCallsListLoading,
    getCallDetailsFromCallIds,
    sendGetTranscriptSegmentsRequest,
    sendUpdateSpeakerIdentities,
    sendResolveSpeakerNames,
    sendUpdateCallSummary,
    sendRegenerateCallSummary,
    sendUpdateCallAgenda,
    setIsCallsListLoading,
    setLiveTranscriptCallId,
    // New date-range-based API (replaces setPeriodsToLoad/periodsToLoad)
    setDateRange,
    dateRange,
    // Server-side total for the current range (null until loaded)
    totalCallCount,
    totalCallCountTruncated,
  };
};

export default useCallsGraphQlApi;
