/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  checklistDefinitions,
  checklistProblem,
  exportableMeeting,
  filterMeetingsByPreparation,
  formatElapsed,
  labelsToText,
  moveItem,
  nextItemId,
  parseLabelFilter,
  parseLabelsText,
  statusSummary,
  vpPreparationChanges,
  vpPreparationForm,
  vpPreparationInput,
  vpPreparationProblem,
} from './meeting-preparation';
import { adoptMeetingCopy, preparationChanged } from './map-call-attributes';
import { mergeCallUpdate } from '../../hooks/use-calls-graphql-api';

describe('labels', () => {
  it('read one key=value per line and write them back the same way', () => {
    const { labels, error } = parseLabelsText(' project = website-relaunch \n\nteam=a\nflag');
    expect(error).toBeNull();
    expect(labels).toEqual([
      { Key: 'project', Value: 'website-relaunch' },
      { Key: 'team', Value: 'a' },
      { Key: 'flag', Value: '' },
    ]);
    expect(labelsToText(labels)).toBe('project=website-relaunch\nteam=a\nflag=');
  });

  it.each([
    ['Project=x', 'not a label key'],
    ['-a=x', 'not a label key'],
    ['a=1\na=2', 'more than once'],
    [`a=${'v'.repeat(257)}`, 'longer than 256'],
    [Array.from({ length: 21 }, (_, i) => `k${i}=v`).join('\n'), 'at most 20 labels'],
  ])('refuse %j as the server would', (text, message) => {
    expect(parseLabelsText(text).error).toContain(message);
  });
});

describe('the meeting list filters', () => {
  const MEETINGS = [
    { callId: 'a', labels: [{ Key: 'project', Value: 'acme' }], audience: 'external' },
    { callId: 'b', labels: [{ Key: 'project', Value: 'other' }], audience: 'internal' },
    { callId: 'c' },
  ];
  const ids = (filters) => filterMeetingsByPreparation(MEETINGS, filters).map((m) => m.callId);

  it('match a label by key=value or by key alone', () => {
    expect(ids({ label: 'project=acme' })).toEqual(['a']);
    expect(ids({ label: 'Project' })).toEqual(['a', 'b']);
    expect(ids({ label: '' })).toEqual(['a', 'b', 'c']);
    expect(parseLabelFilter('  ')).toBeNull();
  });

  it('match an audience, or meetings with none', () => {
    expect(ids({ audience: 'external' })).toEqual(['a']);
    expect(ids({ audience: 'unspecified' })).toEqual(['c']);
    expect(ids({ label: 'project', audience: 'internal' })).toEqual(['b']);
  });

  it('export labels as text and leave the context and checklist out', () => {
    expect(
      exportableMeeting({ callId: 'a', labels: MEETINGS[0].labels, checklist: [{}], meetingContext: 'x' }),
    ).toEqual({ callId: 'a', labels: 'project=acme' });
  });
});

describe('the checklist editor', () => {
  it('gives a new item an unused id', () => {
    expect(nextItemId([])).toBe('item-1');
    expect(nextItemId([{ ItemId: 'item-2' }])).toBe('item-3');
    expect(nextItemId([{ ItemId: 'item-2' }, { ItemId: 'x' }])).toBe('item-3');
  });

  it('moves an item and stops at the ends', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 1)).toEqual(['b', 'a', 'c']);
    expect(moveItem(['a', 'b'], 0, -1)).toEqual(['a', 'b']);
  });

  it('sends only the defining fields, with should as the default priority', () => {
    expect(
      checklistDefinitions([{ ItemId: 'a', Text: ' x ', Kind: 'topic', Status: 'covered', Evidence: [], Source: '' }]),
    ).toEqual([{ ItemId: 'a', Text: 'x', Kind: 'topic', Priority: 'should', Source: '' }]);
  });

  it('says what is wrong before it is sent', () => {
    expect(checklistProblem([{ ItemId: 'a', Text: 'x' }])).toBeNull();
    expect(checklistProblem([{ ItemId: 'a', Text: '  ' }])).toContain('no text');
    expect(checklistProblem([{ ItemId: 'a b', Text: 'x' }])).toContain('id');
    expect(
      checklistProblem([
        { ItemId: 'a', Text: 'x' },
        { ItemId: 'a', Text: 'y' },
      ]),
    ).toContain('share');
    expect(checklistProblem([{ ItemId: 'a', Text: 'x'.repeat(501) }])).toContain('500');
  });

  it('counts the statuses for the header', () => {
    expect(statusSummary([{ Status: 'covered' }, { Status: 'open' }, { Status: 'nonsense' }])).toBe(
      '2 open, 1 covered',
    );
  });

  it('shows evidence times as the meeting page does', () => {
    expect(formatElapsed(64)).toBe('1:04');
    expect(formatElapsed(3725)).toBe('1:02:05');
  });
});

describe('the Virtual Participant forms', () => {
  const VP = {
    labels: [{ Key: 'project', Value: 'acme' }],
    audience: 'external',
    meetingContext: 'Goals',
    checklist: [{ ItemId: 'q1', Text: 'Ask', Kind: 'question', Priority: 'must' }],
  };

  it('send only what was given at creation', () => {
    expect(vpPreparationInput({ labelsText: '', audience: '', meetingContext: '  ', checklist: [] })).toEqual({});
    expect(vpPreparationInput(vpPreparationForm(VP))).toEqual(VP);
  });

  it('send only what an edit changed, and empty to remove it', () => {
    const form = vpPreparationForm(VP);
    expect(vpPreparationChanges(form, VP)).toEqual({});
    expect(vpPreparationChanges({ ...form, audience: '', labelsText: '' }, VP)).toEqual({ audience: '', labels: [] });
    expect(vpPreparationChanges({ ...form, checklist: [] }, VP)).toEqual({ checklist: [] });
    expect(vpPreparationChanges({ ...form, meetingContext: ' ' }, VP)).toEqual({ meetingContext: '' });
  });

  it('refuse what the server would', () => {
    expect(vpPreparationProblem(vpPreparationForm(VP))).toBeNull();
    expect(vpPreparationProblem({ ...vpPreparationForm(VP), labelsText: 'Bad=1' })).toContain('label key');
    expect(vpPreparationProblem({ ...vpPreparationForm(VP), meetingContext: 'x'.repeat(20001) })).toContain('20000');
  });
});

describe('live updates', () => {
  const PREPARED = {
    CallId: 'c',
    Labels: [{ Key: 'k', Value: 'v' }],
    Audience: 'internal',
    MeetingMetadataUpdatedAt: '1',
    MeetingContext: 'ctx',
    MeetingContextUpdatedAt: '1',
    Checklist: [{ ItemId: 'a' }],
    ChecklistUpdatedAt: '1',
  };

  it('keep the preparation over an update that did not select it', () => {
    const merged = mergeCallUpdate(PREPARED, { CallId: 'c', Status: 'ENDED' });
    expect(merged.Labels).toEqual(PREPARED.Labels);
    expect(merged.MeetingContext).toBe('ctx');
    expect(merged.Checklist).toEqual([{ ItemId: 'a' }]);
  });

  it('take a cleared value from an update that carries its timestamp', () => {
    const merged = mergeCallUpdate(PREPARED, {
      CallId: 'c',
      Labels: [],
      Audience: null,
      MeetingMetadataUpdatedAt: '2',
    });
    expect(merged.Labels).toEqual([]);
    expect(merged.Audience).toBeNull();
    expect(merged.Checklist).toEqual([{ ItemId: 'a' }]);
  });

  it('keep the page copy until a newer copy carries a change', () => {
    const current = { callId: 'c', checklist: [{ ItemId: 'a' }], checklistUpdatedAt: '1', meetingContext: 'x' };
    expect(adoptMeetingCopy(current, { callId: 'c' }).checklist).toEqual([{ ItemId: 'a' }]);
    expect(adoptMeetingCopy(current, { callId: 'c', checklist: [], checklistUpdatedAt: '2' }).checklist).toEqual([]);
    expect(preparationChanged(current, { checklistUpdatedAt: '1' })).toBe(false);
    expect(preparationChanged(current, { checklistUpdatedAt: '2' })).toBe(true);
    expect(preparationChanged(current, { meetingMetadataUpdatedAt: '5' })).toBe(true);
  });
});
