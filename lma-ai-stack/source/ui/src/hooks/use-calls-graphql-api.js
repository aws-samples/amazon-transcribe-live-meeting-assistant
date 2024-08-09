// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react';
import { API, Logger, graphqlOperation } from 'aws-amplify';

import useAppContext from '../contexts/app';

import listCallDateShard from '../graphql/queries/listCallDateShard';
import listCallDateHour from '../graphql/queries/listCallDateHour';
import listCalls from '../graphql/queries/listCalls';
import getCall from '../graphql/queries/getCall';

import onCreateCall from '../graphql/queries/onCreateCall';
import onAddTranscriptSegment from '../graphql/queries/onAddTranscriptSegment';
import onUpdateCall from '../graphql/queries/onUpdateCall';
import getTranscriptSegments from '../graphql/queries/getTranscriptSegments';

import { CALL_LIST_SHARDS_PER_DAY } from '../components/call-list/calls-table-config';

const logger = new Logger('useCallsGraphQlApi');

const useCallsGraphQlApi = ({ initialPeriodsToLoad = CALL_LIST_SHARDS_PER_DAY * 2 } = {}) => {
  const [periodsToLoad, setPeriodsToLoad] = useState(initialPeriodsToLoad);
  const [isCallsListLoading, setIsCallsListLoading] = useState(false);
  const [calls, setCalls] = useState([]);
  const [liveTranscriptCallId, setLiveTranscriptCallId] = useState();
  const [callTranscriptPerCallId, setCallTranscriptPerCallId] = useState({});
  const { setErrorMessage } = useAppContext();

  const setCallsDeduped = (callValues) => {
    setCalls((currentCalls) => {
      const callValuesCallIds = callValues.map((c) => c.CallId);
      return [...currentCalls.filter((c) => !callValuesCallIds.includes(c.CallId)), ...callValues];
    });
  };

  const getCallDetailsFromCallIds = async (callIds) => {
    // prettier-ignore
    const getCallPromises = callIds.map((callId) => (
      API.graphql({ query: getCall, variables: { callId } })
    ));
    const getCallResolutions = await Promise.allSettled(getCallPromises);
    const getCallRejected = getCallResolutions.filter((r) => r.status === 'rejected');
    if (getCallRejected.length) {
      setErrorMessage('failed to get call details - please try again later');
      logger.error('get call promises rejected', getCallRejected);
    }
    const callValues = getCallResolutions.filter((r) => r.status === 'fulfilled').map((r) => r.value?.data?.getCall);

    return callValues;
  };

  useEffect(() => {
    logger.debug('onCreateCall subscription');
    const subscription = API.graphql(graphqlOperation(onCreateCall)).subscribe({
      next: async ({ provider, value }) => {
        logger.debug('call list subscription update', { provider, value });
        const callId = value?.data?.onCreateCall.CallId || '';
        if (callId) {
          const callValues = await getCallDetailsFromCallIds([callId]);
          setCallsDeduped(callValues);
        }
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
    const subscription = API.graphql(graphqlOperation(onUpdateCall)).subscribe({
      next: async ({ provider, value }) => {
        logger.debug('call update', { provider, value });
        const callUpdateEvent = value?.data?.onUpdateCall;
        if (callUpdateEvent?.CallId) {
          setCallsDeduped([callUpdateEvent]);
        }
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('call update network request failed - please reload the page');
      },
    });

    return () => subscription.unsubscribe();
  }, []);

  const handleCallTranscriptSegmentMessage = (transcriptSegment) => {
    const { callId, transcript, isPartial, channel } = transcriptSegment;

    setCallTranscriptPerCallId((current) => {
      logger.debug('setCallTrancriptPerCallId current: ', current);

      const currentContactEntry = current[callId] || {};
      const currentChannelEntry = currentContactEntry[channel] || {};

      const currentBase = currentChannelEntry?.base || '';
      const currentSegments = currentChannelEntry?.segments || [];
      logger.debug('setCallTrancriptPerCallId current segments: ', currentSegments);
      const lastSameSegmentId = currentSegments.filter((s) => s.segmentId === transcriptSegment.segmentId).pop();
      const dedupedSegments = currentSegments.filter((s) => s.segmentId !== transcriptSegment.segmentId);

      const segments = [
        ...dedupedSegments,
        // prettier-ignore
        // avoid overwriting a final segment or one with sentiment with a late arriving segment
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
      logger.debug('setCallTrancriptPerCallId new contact id entry: ', entry);

      return {
        ...current,
        [callId]: { ...entry },
      };
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
      // the component should set the live transcript contact id to null to unsubscribe
      if (subscription?.unsubscribe) {
        logger.debug('onAddTranscriptSegment null contact unsubscribing');
        subscription.unsubscribe();
      }
      return () => {};
    }
    logger.debug('setting up onAddTranscriptSegment subscription');

    subscription = API.graphql(graphqlOperation(onAddTranscriptSegment, { callId: liveTranscriptCallId })).subscribe({
      next: async ({ provider, value }) => {
        logger.debug('call transcript subscription update', { provider, value });
        const transcriptSegmentValue = value?.data?.onAddTranscriptSegment;
        if (!transcriptSegmentValue) {
          return;
        }
        const transcriptSegment = mapTranscriptSegmentValue(transcriptSegmentValue);
        const { callId, transcript, segmentId } = transcriptSegment;
        if (callId !== liveTranscriptCallId) {
          return;
        }
        if (transcript && segmentId) {
          handleCallTranscriptSegmentMessage(transcriptSegment);
        }
      },
      error: (error) => {
        logger.error(error);
        setErrorMessage('transcript update network subscription failed - please reload the page');
      },
    });

    return () => {
      logger.debug('unsubscribed from transcript segments');
      subscription.unsubscribe();
    };
  }, [liveTranscriptCallId]);

  const listCallIdsByDateShards = async ({ date, shards }) => {
    const listCallDateShardPromises = shards.map((i) => {
      logger.debug('sendig list call date shard', date, i);
      return API.graphql({ query: listCallDateShard, variables: { date, shard: i } });
    });
    const listCallDateShardResolutions = await Promise.allSettled(listCallDateShardPromises);

    const listRejected = listCallDateShardResolutions.filter((r) => r.status === 'rejected');
    if (listRejected.length) {
      setErrorMessage('failed to list calls - please try again later');
      logger.error('list call promises rejected', listRejected);
    }

    const callIds = listCallDateShardResolutions
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value?.data?.listCallsDateShard?.Calls || [])
      .map((items) => items.map((item) => item?.CallId))
      .reduce((pv, cv) => [...cv, ...pv], []);

    return callIds;
  };

  const listCallIdsByDateHours = async ({ date, hours }) => {
    const listCallDateHourPromises = hours.map((i) => {
      logger.debug('sendig list call date hour', date, i);
      return API.graphql({ query: listCallDateHour, variables: { date, hour: i } });
    });
    const listCallDateHourResolutions = await Promise.allSettled(listCallDateHourPromises);

    const listRejected = listCallDateHourResolutions.filter((r) => r.status === 'rejected');
    if (listRejected.length) {
      setErrorMessage('failed to list calls - please try again later');
      logger.error('list call promises rejected', listRejected);
    }

    const callIds = listCallDateHourResolutions
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value?.data?.listCallsDateHour?.Calls || [])
      .map((items) => items.map((item) => item?.CallId))
      .reduce((pv, cv) => [...cv, ...pv], []);

    return callIds;
  };

  // eslint-disable-next-line no-unused-vars
  const listCallIds = async () => {
    // this uses a Scan of dynamoDB - prefer using the shard based queries
    const listCallsPromise = API.graphql({ query: listCalls });
    const listCallsResolutions = await Promise.allSettled([listCallsPromise]);

    const listRejected = listCallsResolutions.filter((r) => r.status === 'rejected');
    if (listRejected.length) {
      setErrorMessage('failed to list calls - please try again later');
      logger.error('list call promises rejected', listRejected);
    }

    const callIds = listCallsResolutions
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value?.data?.listCalls?.Calls || [])
      .map((items) => items.map((item) => item?.CallId))
      .reduce((pv, cv) => [...cv, ...pv], []);

    return callIds;
  };

  const sendSetCallsForPeriod = async () => {
    // XXX this logic should be moved to the API
    try {
      const now = new Date();

      // array of arrays containing date / shard pairs relative to current UTC time
      // e.g. 2 periods to on load 2021-01-01T:20:00:00.000Z ->
      // [ [ '2021-01-01', 3 ], [ '2021-01-01', 4 ] ]
      const hoursInShard = 24 / CALL_LIST_SHARDS_PER_DAY;
      const dateShardPairs = [...Array(parseInt(periodsToLoad, 10)).keys()].map((p) => {
        const deltaInHours = p * hoursInShard;
        const relativeDate = new Date(now - deltaInHours * 3600 * 1000);

        const relativeDateString = relativeDate.toISOString().split('T')[0];
        const shard = Math.floor(relativeDate.getUTCHours() / hoursInShard);

        return [relativeDateString, shard];
      });

      // reduce array of date/shard pairs into object of shards by date
      // e.g. [ [ '2021-01-01', 3 ], [ '2021-01-01', 4 ] ] -> { '2021-01-01': [ 3, 4 ] }
      const dateShards = dateShardPairs.reduce((p, c) => ({ ...p, [c[0]]: [...(p[c[0]] || []), c[1]] }), {});
      logger.debug('call list date shards', dateShards);

      // parallelizes listCalls and getCallDetails
      // alternatively we could implement it by sending multiple graphql queries in 1 request
      const callIdsDateShardPromises = Object.keys(dateShards).map(
        // pretttier-ignore
        async (d) => listCallIdsByDateShards({ date: d, shards: dateShards[d] }),
      );

      // get contact Ids by hour on residual hours outside of the lower shard date/hour boundary
      // or just last n hours when periodsToLoad is less than 1 shard period
      let baseDate;
      let residualHours;
      if (periodsToLoad < 1) {
        baseDate = new Date(now);
        const numHours = parseInt(periodsToLoad * hoursInShard, 10);
        residualHours = [...Array(numHours).keys()].map((h) => baseDate.getUTCHours() - h);
      } else {
        baseDate = new Date(now - periodsToLoad * hoursInShard * 3600 * 1000);
        const residualBaseHour = baseDate.getUTCHours() % hoursInShard;
        residualHours = [...Array(hoursInShard - residualBaseHour).keys()].map((h) => baseDate.getUTCHours() + h);
      }
      const baseDateString = baseDate.toISOString().split('T')[0];

      const residualDateHours = { date: baseDateString, hours: residualHours };
      logger.debug('call list date hours', residualDateHours);
      const callIdsDateHourPromise = listCallIdsByDateHours(residualDateHours);

      const callIdsPromises = [...callIdsDateShardPromises, callIdsDateHourPromise];
      const callDetailsPromises = callIdsPromises.map(async (callIdsPromise) => {
        const callIds = await callIdsPromise;
        logger.debug('callIds', callIds);
        return getCallDetailsFromCallIds(callIds);
      });
      const callValuesPromises = callDetailsPromises.map(async (callValuesPromise) => {
        const callValues = await callValuesPromise;
        logger.debug('callValues', callValues);
        return callValues;
      });

      const getCallsPromiseResolutions = await Promise.allSettled(callValuesPromises);
      logger.debug('getCallsPromiseResolutions', getCallsPromiseResolutions);
      const callValuesReduced = getCallsPromiseResolutions
        .filter((r) => r.status === 'fulfilled')
        .map((r) => r.value)
        .reduce((previous, current) => [...previous, ...current], []);
      logger.debug('callValuesReduced', callValuesReduced);
      setCallsDeduped(callValuesReduced);
      setIsCallsListLoading(false);
      const getCallsRejected = getCallsPromiseResolutions.filter((r) => r.status === 'rejected');
      if (getCallsRejected.length) {
        setErrorMessage('failed to get call details - please try again later');
        logger.error('get call promises rejected', getCallsRejected);
      }
    } catch (error) {
      setIsCallsListLoading(false);
      setErrorMessage('failed to list calls - please try again later');
      logger.error('error obtaining call list', error);
    }
  };

  useEffect(() => {
    if (isCallsListLoading) {
      logger.debug('call list is loading');
      // send in a timeout to avoid blocking rendering
      setTimeout(() => {
        setCalls([]);
        sendSetCallsForPeriod();
      }, 1);
    }
  }, [isCallsListLoading]);

  useEffect(() => {
    logger.debug('list period changed', periodsToLoad);
    setIsCallsListLoading(true);
  }, [periodsToLoad]);

  const sendGetTranscriptSegmentsRequest = async (callId) => {
    try {
      const response = await API.graphql({
        query: getTranscriptSegments,
        variables: { callId },
      });
      const transcriptSegments = response?.data?.getTranscriptSegments?.TranscriptSegments;
      logger.debug('transcript segments response', transcriptSegments);
      if (transcriptSegments?.length > 0) {
        const transcriptSegmentsReduced = transcriptSegments
          .map((t) => mapTranscriptSegmentValue(t))
          .reduce((p, c) => {
            const previousSegments = p[c.channel]?.segments || [];
            const lastSameSegmentId = previousSegments.filter((s) => s?.segmentId === c?.segmentId).pop();
            const dedupedSegments = previousSegments.filter((s) => s.segmentId !== c.segmentId);

            // prettier-ignore
            const segment = !lastSameSegmentId?.sentiment && c?.sentiment
              ? c
              : lastSameSegmentId || c;

            return { ...p, [c.channel]: { segments: [...dedupedSegments, segment] } };
          }, {});

        setCallTranscriptPerCallId((current) => {
          logger.debug('updating callTranscriptPerCallId', current, transcriptSegmentsReduced);
          return {
            ...current,
            [callId]: transcriptSegmentsReduced,
          };
        });
      }
    } catch (error) {
      setErrorMessage('failed to get transcript - please try again later');
      logger.error('failed to set transcript segments', error);
    }
  };

  return {
    calls,
    callTranscriptPerCallId,
    isCallsListLoading,
    getCallDetailsFromCallIds,
    sendGetTranscriptSegmentsRequest,
    setIsCallsListLoading,
    setLiveTranscriptCallId,
    setPeriodsToLoad,
    periodsToLoad,
  };
};

export default useCallsGraphQlApi;
