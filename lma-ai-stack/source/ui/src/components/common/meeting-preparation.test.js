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
  LABEL_FILTER_KEY_PROBLEMS,
  labelFilterProblem,
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
    const { labels, error } = parseLabelsText('project=website-relaunch\n\n  \nteam=a\nflag');
    expect(error).toBeNull();
    expect(labels).toEqual([
      { Key: 'project', Value: 'website-relaunch' },
      { Key: 'team', Value: 'a' },
      { Key: 'flag', Value: '' },
    ]);
    expect(labelsToText(labels)).toBe('project=website-relaunch\nteam=a\nflag=');
  });

  it('keep a value exactly as typed, spaces, case and "=" included', () => {
    const stored = [
      { Key: 'customer', Value: '  Opaque ACME Corp ' },
      { Key: 'region', Value: 'ÜBER-Nord' },
      { Key: 'query', Value: 'a=b, c' },
      { Key: 'empty', Value: '' },
    ];
    const { labels, error } = parseLabelsText(labelsToText(stored));
    expect(error).toBeNull();
    // Opening the editor and saving it unchanged sends the labels as stored.
    expect(labels).toEqual(stored);
    expect(labelsToText(labels)).toBe(labelsToText(stored));
  });

  it.each([' project=x', 'project =x', 'Project=x'])(
    'refuse the key in %j rather than trim or lowercase it',
    (text) => {
      const { labels, error } = parseLabelsText(text);
      expect(error).toContain('not a label key');
      expect(labels).toEqual([]);
    },
  );

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
    expect(ids({ label: 'project' })).toEqual(['a', 'b']);
    expect(ids({ label: '' })).toEqual(['a', 'b', 'c']);
    expect(parseLabelFilter('  ')).toBeNull();
  });

  it('match a label exactly as typed, without trimming or lowercasing it', () => {
    const padded = [{ callId: 'p', labels: [{ Key: 'customer', Value: '  Opaque X ' }] }];
    const match = (label) => filterMeetingsByPreparation(padded, { label }).map((m) => m.callId);
    expect(parseLabelFilter('customer=  Opaque X ')).toEqual({ key: 'customer', value: '  Opaque X ' });
    expect(match('customer=  Opaque X ')).toEqual(['p']);
    expect(match('customer=Opaque X')).toEqual([]);
    expect(match('customer=  opaque x ')).toEqual([]);
    expect(ids({ label: 'Project' })).toEqual([]);
    expect(ids({ label: 'project=ACME' })).toEqual([]);
  });

  it('say which rule a filter key that is not a label key breaks, short enough for the toolbar', () => {
    const { missing, tooLong, start, characters } = LABEL_FILTER_KEY_PROBLEMS;
    expect(labelFilterProblem('project=acme')).toBeNull();
    expect(labelFilterProblem('project')).toBeNull();
    expect(labelFilterProblem('')).toBeNull();
    expect(labelFilterProblem(`a${'b'.repeat(62)}`)).toBeNull();
    expect(labelFilterProblem('Project=acme')).toBe(characters);
    expect(labelFilterProblem('project ')).toBe(characters);
    expect(labelFilterProblem('my project')).toBe(characters);
    expect(labelFilterProblem('-acme')).toBe(start);
    expect(labelFilterProblem('.acme=x')).toBe(start);
    expect(labelFilterProblem(`a${'b'.repeat(63)}`)).toBe(tooLong);
    expect(labelFilterProblem('=acme')).toBe(missing);
    // A key the filter refuses is one the label editor refuses too.
    ['Project', 'project ', 'my project', '-acme', `a${'b'.repeat(63)}`].forEach((key) =>
      expect(parseLabelsText(`${key}=x`).error).toMatch(/is not a label key/),
    );
    Object.values(LABEL_FILTER_KEY_PROBLEMS).forEach((message) => expect(message.length).toBeLessThanOrEqual(60));
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

  it('send a summary profile only when one other than the default is chosen', () => {
    expect(vpPreparationInput({ ...vpPreparationForm(VP), summaryProfile: 'discovery-session' })).toEqual({
      ...VP,
      summaryProfile: 'discovery-session',
    });
    expect(vpPreparationInput({ ...vpPreparationForm(VP), summaryProfile: 'default' })).toEqual(VP);
    const scheduled = { ...VP, summaryProfile: 'discovery-session' };
    const form = vpPreparationForm(scheduled);
    expect(form.summaryProfile).toBe('discovery-session');
    expect(vpPreparationChanges(form, scheduled)).toEqual({});
    // Back to the default: sent empty, which removes it.
    expect(vpPreparationChanges({ ...form, summaryProfile: '' }, scheduled)).toEqual({ summaryProfile: '' });
    expect(vpPreparationChanges({ ...vpPreparationForm(VP), summaryProfile: 'healthcare' }, VP)).toEqual({
      summaryProfile: 'healthcare',
    });
  });

  it('refuse what the server would', () => {
    expect(vpPreparationProblem(vpPreparationForm(VP))).toBeNull();
    expect(vpPreparationProblem({ ...vpPreparationForm(VP), labelsText: 'Bad=1' })).toContain('label key');
    expect(vpPreparationProblem({ ...vpPreparationForm(VP), meetingContext: 'x'.repeat(20001) })).toContain('20000');
    expect(vpPreparationProblem({ ...vpPreparationForm(VP), participantsText: 'Ana <ana@example>' })).toContain(
      'no valid email address',
    );
  });

  it('send the participant list at creation only when one is given', () => {
    const form = { ...vpPreparationForm(VP), participantsText: 'Ana Lima <ana@example.com>\nbo@example.org' };
    expect(vpPreparationInput(form)).toEqual({
      ...VP,
      participants: [
        { name: 'Ana Lima', email: 'ana@example.com' },
        { name: '', email: 'bo@example.org' },
      ],
    });
    expect(vpPreparationInput({ ...form, participantsText: '  ' })).toEqual(VP);
  });

  it('send the participant list in an edit only when it changed, and empty to remove it', () => {
    const scheduled = { ...VP, participants: [{ name: 'Ana Lima', email: 'ana@example.com' }] };
    const form = vpPreparationForm(scheduled);
    expect(form.participantsText).toBe('Ana Lima <ana@example.com>');
    expect(vpPreparationChanges(form, scheduled)).toEqual({});
    expect(vpPreparationChanges({ ...form, participantsText: '' }, scheduled)).toEqual({ participants: [] });
    expect(vpPreparationChanges({ ...form, participantsText: 'Ana <ana@example.com>' }, scheduled)).toEqual({
      participants: [{ name: 'Ana', email: 'ana@example.com' }],
    });
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
