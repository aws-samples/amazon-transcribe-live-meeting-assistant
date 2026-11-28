/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting page's playbook panel: a suggestion is shown and never applied by
 * itself; choosing one sends the playbook change (applyMeetingPlaybook) with its
 * skill id; the applied playbook says what it turned on; removing it can delete
 * the whiteboards nobody wrote on; and someone who may not choose sees which
 * one runs, without the controls. See docs/meeting-playbooks.md.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import MeetingPlaybookPanel from './MeetingPlaybookPanel';
import { PREPARATION_MUTATIONS } from '../../hooks/use-calls-graphql-api';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const SPRINT = {
  SkillId: 'sprint-planning',
  Name: 'sprint-planning',
  DisplayName: 'Sprint planning',
  Description: 'Playbook for sprint planning.',
  Whiteboards: [
    { template: 'user_stories', templateLabel: 'User stories', title: 'Sprint backlog', assistantUpdates: true },
  ],
  Guidance: {
    checks: [
      { check: 'action_missing_owner', label: 'Actions without an owner', available: true },
      { check: 'fact_check', label: 'Fact checks', available: false },
    ],
    assertiveness: 'balanced',
  },
  QuickActions: [{ label: 'Scope vs capacity', prompt: 'Add up the estimates.' }],
  Estimate: { lines: [{}], perHourAtMost: 0.06, perMeetingAtMost: 0.17, unpriced: [] },
  Notes: [],
};
const RETRO = { ...SPRINT, SkillId: 'retrospective', Name: 'retrospective', DisplayName: 'Retrospective' };

const clientWith = (options) => ({
  graphql: vi.fn(async () => ({ data: { meetingPlaybookOptions: JSON.stringify(options) } })),
});

const OPTIONS = {
  Playbooks: [SPRINT, RETRO],
  Suggestions: [{ SkillId: 'sprint-planning', DisplayName: 'Sprint planning', Reason: 'The title mentions it.' }],
  ProfileDefault: null,
  Current: null,
};

const ITEM = { callId: 'Sprint 42 planning - 2026', playbook: null };

describe('MeetingPlaybookPanel', () => {
  it('shows a suggestion and applies nothing until the owner applies it', async () => {
    const onApply = vi.fn(async () => ({ ok: true }));
    const client = clientWith(OPTIONS);
    render(<MeetingPlaybookPanel item={ITEM} canChoose isLive onApply={onApply} client={client} />);
    expect(await screen.findByText('Suggested: Sprint planning')).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    const dialog = await screen.findByRole('dialog', { name: 'Choose a meeting playbook' });
    // What it does and costs, and the check this deployment has off.
    expect(within(dialog).getByText('Sprint backlog')).toBeInTheDocument();
    expect(within(dialog).getByText(/Fact checks is turned off for this deployment/)).toBeInTheDocument();
    expect(within(dialog).getByText(/up to about \$0.06 per meeting-hour/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByTestId('apply-playbook'));
    await waitFor(() => expect(onApply).toHaveBeenCalledWith({ SkillId: 'sprint-planning', DeleteWhiteboards: false }));
    expect(PREPARATION_MUTATIONS.playbook[0]).toBe('applyMeetingPlaybook');
  });

  it('says what the applied playbook turned on, and removes it with its untouched whiteboards', async () => {
    const onApply = vi.fn(async () => ({ ok: true }));
    const item = {
      ...ITEM,
      playbook: {
        SkillId: 'sprint-planning',
        DisplayName: 'Sprint planning',
        Origin: 'virtual-participant',
        Whiteboards: [{ WhiteboardId: 'w1', Title: 'Sprint backlog', Template: 'user_stories' }],
        Guidance: { Checks: ['action_missing_owner'] },
        QuickActions: [{ Label: 'Scope', Prompt: 'Add up.' }],
        Notes: ['Fact checks are turned off for this deployment, so they do not run.'],
      },
    };
    render(<MeetingPlaybookPanel item={item} canChoose isLive onApply={onApply} client={clientWith(OPTIONS)} />);
    expect(screen.getByText(/chosen when the Virtual Participant was set up/)).toBeInTheDocument();
    expect(
      screen.getByText(/1 whiteboard \(Sprint backlog\); live guidance: Actions without an owner/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Fact checks are turned off/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('remove-playbook'));
    fireEvent.click(await screen.findByRole('checkbox', { name: /delete the whiteboards it made/i }));
    fireEvent.click(screen.getByTestId('confirm-remove-playbook'));
    await waitFor(() => expect(onApply).toHaveBeenCalledWith({ SkillId: null, DeleteWhiteboards: true }));
  });

  it('shows someone who may not choose which playbook runs, without the controls', () => {
    const item = { ...ITEM, playbook: { SkillId: 'retrospective', DisplayName: 'Retrospective', Origin: 'chosen' } };
    render(<MeetingPlaybookPanel item={item} onApply={vi.fn()} client={clientWith(OPTIONS)} />);
    expect(screen.getByText('Retrospective')).toBeInTheDocument();
    expect(screen.queryByTestId('choose-playbook')).toBeNull();
    expect(screen.queryByTestId('remove-playbook')).toBeNull();
  });

  it('shows nothing to someone who may not choose when the meeting runs none', () => {
    const { container } = render(<MeetingPlaybookPanel item={ITEM} onApply={vi.fn()} client={clientWith(OPTIONS)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says why a playbook chosen for the meeting was not applied', () => {
    const item = {
      ...ITEM,
      playbook: { Requested: 'gone', Notes: ['The playbook “gone” chosen for this meeting no longer exists.'] },
    };
    render(<MeetingPlaybookPanel item={item} canChoose isLive onApply={vi.fn()} client={clientWith(OPTIONS)} />);
    expect(screen.getByText(/no longer exists/)).toBeInTheDocument();
    expect(screen.getByText(/No playbook/)).toBeInTheDocument();
  });

  it('offers no new playbook once the meeting has ended', async () => {
    render(
      <MeetingPlaybookPanel item={ITEM} canChoose isLive={false} onApply={vi.fn()} client={clientWith(OPTIONS)} />,
    );
    expect(screen.getByTestId('choose-playbook')).toBeDisabled();
    expect(screen.getByText('A playbook applies to a meeting in progress.')).toBeInTheDocument();
  });
});
