/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import mapCallsAttributes, { adoptMeetingCopy, agendaChanged } from './map-call-attributes';

const call = (sentiment) => ({ CallId: 'c1', CreatedAt: '2026-01-01T00:00:00Z', Sentiment: sentiment });
const quarters = (score) => [1, 2, 3, 4].map((i) => ({ BeginOffsetMillis: i, EndOffsetMillis: i + 1, Score: score }));

describe('mapCallsAttributes sentiment', () => {
  it('reads the REMOTE and LOCAL sentiment a meeting is written with', () => {
    const [item] = mapCallsAttributes([
      call({
        OverallSentiment: { REMOTE: 2, LOCAL: -3, CALLER: null, AGENT: null },
        SentimentByPeriod: { QUARTER: { REMOTE: quarters(2), LOCAL: quarters(-3), CALLER: null, AGENT: null } },
      }),
    ]);
    expect(item.callerAverageSentiment).toBe(2);
    expect(item.agentAverageSentiment).toBe(-3);
    expect(item.sentiment.SentimentByPeriod.QUARTER.REMOTE).toEqual(quarters(2));
  });

  it('reads a meeting stored before 0.4.0 with CALLER and AGENT sentiment as REMOTE and LOCAL', () => {
    const [item] = mapCallsAttributes([
      call({
        OverallSentiment: { REMOTE: null, LOCAL: null, CALLER: 1.5, AGENT: 0.5 },
        SentimentByPeriod: { QUARTER: { REMOTE: null, LOCAL: null, CALLER: quarters(1.5), AGENT: quarters(0.5) } },
      }),
    ]);
    expect(item.callerAverageSentiment).toBe(1.5);
    expect(item.agentAverageSentiment).toBe(0.5);
    // The charts index the quarters by the transcript's channel, which is REMOTE/LOCAL.
    expect(item.sentiment.SentimentByPeriod.QUARTER.REMOTE).toEqual(quarters(1.5));
    expect(item.sentiment.SentimentByPeriod.QUARTER.LOCAL).toEqual(quarters(0.5));
  });

  it('maps a meeting with no sentiment', () => {
    const [item] = mapCallsAttributes([call(undefined)]);
    expect(item.callerAverageSentiment).toBe(0);
    expect(item.agentAverageSentiment).toBe(0);
  });
});

describe('adoptMeetingCopy', () => {
  it('maps live guidance and keeps it through a copy that leaves it out', () => {
    const [mapped] = mapCallsAttributes([
      {
        CallId: 'c1',
        GuidanceEnabled: true,
        CreatedAt: '2026-11-03T14:05:09.123Z',
        UpdatedAt: '2026-11-03T14:30:00.000Z',
      },
    ]);
    expect(mapped.guidanceEnabled).toBe(true);
    expect(adoptMeetingCopy(mapped, { callId: 'c1' }).guidanceEnabled).toBe(true);
    expect(adoptMeetingCopy(mapped, { callId: 'c1', guidanceEnabled: false }).guidanceEnabled).toBe(false);
  });

  const AGENDA = {
    agenda: 'Budget\nHiring',
    agendaSource: 'scheduled',
    agendaUpdatedBy: 'alice',
    agendaUpdatedAt: '2026-11-08T15:13:11.973Z',
  };
  const onScreen = {
    callId: 'c1',
    updatedAt: '2026-11-08T15:20:00Z',
    ...AGENDA,
    captureSource: 'virtual-participant',
    transcriptionEngine: 'transcribe',
  };

  it('keeps the agenda and capture context when the newer copy carries none', () => {
    const live = { callId: 'c1', updatedAt: '2026-11-08T15:21:00Z', agenda: undefined, agendaUpdatedAt: null };
    const next = adoptMeetingCopy(onScreen, live);
    expect(next).toMatchObject({ ...AGENDA, updatedAt: '2026-11-08T15:21:00Z' });
    expect(next.captureSource).toBe('virtual-participant');
    expect(next.transcriptionEngine).toBe('transcribe');
  });

  it('keeps the summary profile and action items from a copy that did not select them', () => {
    const shown = { ...onScreen, summaryProfile: 'discovery-session', actionItems: [{ Text: 'Send it' }] };
    const fromList = { callId: 'c1', summaryProfile: undefined, actionItems: undefined };
    expect(adoptMeetingCopy(shown, fromList)).toMatchObject({
      summaryProfile: 'discovery-session',
      actionItems: [{ Text: 'Send it' }],
    });
    // A write that cleared the profile selects it as null, which is kept.
    expect(adoptMeetingCopy(shown, { callId: 'c1', summaryProfile: null }).summaryProfile).toBeNull();
  });

  it('maps the summary profile, the action items and the platform', () => {
    const [mapped] = mapCallsAttributes([
      {
        CallId: 'c1',
        CreatedAt: '2026-11-08T15:00:00Z',
        UpdatedAt: '2026-11-08T15:30:00Z',
        SummaryProfile: 'discovery-session',
        ActionItems: [{ Text: 'a' }],
        MeetingPlatform: 'Zoom',
      },
    ]);
    expect(mapped).toMatchObject({
      summaryProfile: 'discovery-session',
      actionItems: [{ Text: 'a' }],
      meetingPlatform: 'Zoom',
    });
  });

  it('takes the agenda from a copy that carries one, including a cleared agenda', () => {
    const cleared = { callId: 'c1', agenda: '', agendaSource: 'ui', agendaUpdatedAt: '2026-11-08T15:30:00Z' };
    expect(adoptMeetingCopy(onScreen, cleared)).toMatchObject({ agenda: '', agendaSource: 'ui' });
    expect(agendaChanged(onScreen, cleared)).toBe(true);
  });

  it('reports an agenda change only for a copy with a different agenda timestamp', () => {
    expect(agendaChanged(onScreen, { callId: 'c1' })).toBe(false);
    expect(agendaChanged(onScreen, { ...onScreen })).toBe(false);
  });

  it('takes the newer copy as it is when there is nothing on screen', () => {
    const live = { callId: 'c1' };
    expect(adoptMeetingCopy(null, live)).toBe(live);
  });
});
