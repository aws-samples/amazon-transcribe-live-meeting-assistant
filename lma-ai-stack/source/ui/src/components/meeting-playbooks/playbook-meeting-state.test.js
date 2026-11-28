/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's playbook as the page holds it: an update that says when the
 * playbook changed is the value, a removed playbook included; one that does not
 * select it (the Kinesis pipeline's, the list query) keeps the copy held. See
 * docs/meeting-playbooks.md.
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const { mergeCallUpdate } = await import('../../hooks/use-calls-graphql-api');
const { adoptMeetingCopy, preparationChanged } = await import('../common/map-call-attributes');

const PLAYBOOK = { SkillId: 'sprint-planning', DisplayName: 'Sprint planning' };

describe('the playbook on the page', () => {
  it('keeps the stored playbook through an update that does not select it', () => {
    const existing = { CallId: 'c1', Playbook: PLAYBOOK, PlaybookUpdatedAt: '2026-11-28T10:00:00Z' };
    const merged = mergeCallUpdate(existing, { CallId: 'c1', Status: 'TRANSCRIBING', Playbook: null });
    expect(merged.Playbook).toEqual(PLAYBOOK);
  });

  it('takes a removed playbook from an update that says when it changed', () => {
    const existing = { CallId: 'c1', Playbook: PLAYBOOK, PlaybookUpdatedAt: '2026-11-28T10:00:00Z' };
    const merged = mergeCallUpdate(existing, {
      CallId: 'c1',
      Playbook: null,
      PlaybookUpdatedAt: '2026-11-28T10:05:00Z',
    });
    expect(merged.Playbook).toBeNull();
    expect(merged.PlaybookUpdatedAt).toBe('2026-11-28T10:05:00Z');
  });

  it("adopts a newer copy's playbook only when it names its change", () => {
    const current = { callId: 'c1', playbook: PLAYBOOK, playbookUpdatedAt: 't1' };
    expect(adoptMeetingCopy(current, { callId: 'c1', playbook: null }).playbook).toEqual(PLAYBOOK);
    expect(adoptMeetingCopy(current, { callId: 'c1', playbook: null, playbookUpdatedAt: 't2' }).playbook).toBeNull();
    expect(preparationChanged(current, { playbookUpdatedAt: 't2' })).toBe(true);
  });
});
