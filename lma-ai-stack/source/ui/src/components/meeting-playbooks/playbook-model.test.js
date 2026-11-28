/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The page's side of meeting playbooks: the editor's form round-trips the
 * stored section, the checks mirror the server's, the meeting's quick actions
 * become chat buttons, the cost is said in words, and the Virtual Participant
 * form's choice is sent as the resolvers expect. See docs/meeting-playbooks.md.
 */
import { describe, expect, it } from 'vitest';
import {
  GUIDANCE_CHECKS,
  costSentence,
  describeSection,
  emptyPlaybook,
  isPlaybookSkill,
  playbookForm,
  playbookProblems,
  playbookSection,
  quickActionButtons,
} from './playbook-model';
import {
  describePlaybookChoice,
  playbookChoiceOf,
  playbookCreateInput,
  playbookEditInput,
  playbookSelectOptions,
} from './playbook-choice';
import { savedMetadata, skillForm, skillInput } from '../skills-page/skills-api';

const STORED = {
  suggestFor: ['sprint planning', 'iteration planning'],
  whiteboards: [
    { template: 'user_stories', title: 'Sprint backlog', instructions: 'Stories.', assistantUpdates: true },
  ],
  guidance: { checks: ['action_missing_owner', 'risk_probe'], assertiveness: 'quiet', maxItemsPer10Min: 6 },
  quickActions: [{ label: 'Scope', prompt: 'Add up the estimates.' }],
};

describe('the editor form', () => {
  it('round-trips a stored section', () => {
    expect(playbookSection(playbookForm(STORED))).toEqual(STORED);
  });

  it('drops a guidance part that names no check, and trims what was typed', () => {
    const form = playbookForm(STORED);
    form.guidance.checks = [];
    form.quickActions[0].label = '  Scope  ';
    form.suggestForText = ' sprint planning ,, retro ';
    const section = playbookSection(form);
    expect(section.guidance).toBeUndefined();
    expect(section.quickActions[0].label).toBe('Scope');
    expect(section.suggestFor).toEqual(['sprint planning', 'retro']);
  });

  it('starts a new playbook with a whiteboard and the action checks, which may be saved', () => {
    expect(playbookProblems(playbookSection(playbookForm(emptyPlaybook())))).toEqual([]);
  });

  it('lists every problem the server would refuse', () => {
    const problems = playbookProblems({
      whiteboards: [{ template: 'kanban', title: 'x'.repeat(121) }],
      guidance: { checks: ['sentiment_alarm'], assertiveness: 'loud', maxItemsPer10Min: 40 },
      quickActions: [{ label: '', prompt: 'p' }],
    });
    expect(problems.join(' ')).toMatch(/kanban/);
    expect(problems.join(' ')).toMatch(/longer than 120/);
    expect(problems.join(' ')).toMatch(/sentiment_alarm/);
    expect(problems.join(' ')).toMatch(/assertive/);
    expect(problems.join(' ')).toMatch(/from 1 to 20/);
    expect(problems.join(' ')).toMatch(/needs a label/);
  });

  it('refuses a playbook that turns nothing on', () => {
    expect(playbookProblems({ whiteboards: [], quickActions: [] })).toHaveLength(1);
  });

  it('offers the nine live guidance checks', () => {
    expect(GUIDANCE_CHECKS.map((c) => c.value)).toEqual([
      'ask_question',
      'uncovered_topic',
      'clarify',
      'risk_probe',
      'action_missing_owner',
      'action_missing_due_date',
      'time_check',
      'fact_check',
      'lookup',
    ]);
  });

  it('previews what applying it does without a cost', () => {
    const preview = describeSection(STORED, { displayName: 'Sprint planning' });
    expect(preview.Whiteboards[0]).toEqual({
      template: 'user_stories',
      templateLabel: 'User stories',
      title: 'Sprint backlog',
      assistantUpdates: true,
    });
    expect(preview.Guidance.checks.map((c) => c.label)).toEqual(['Actions without an owner', 'Risks to probe']);
    expect(preview.Estimate).toBeUndefined();
  });
});

describe('the skill editor', () => {
  const skill = {
    Name: 'sprint-planning',
    Description: 'Playbook.',
    Instructions: 'Steps.',
    Metadata: JSON.stringify({ license: 'MIT-0', playbook: STORED }),
  };

  it('holds the playbook section apart from the other metadata and saves it back', () => {
    const form = skillForm(skill);
    expect(form.playbook).not.toBeNull();
    expect(JSON.parse(skillInput(form).Metadata)).toEqual({ license: 'MIT-0', playbook: STORED });
    expect(isPlaybookSkill(skill)).toBe(true);
  });

  it('saves no playbook section once the switch is off', () => {
    const form = { ...skillForm(skill), playbook: null };
    expect(savedMetadata(form)).toEqual({ license: 'MIT-0' });
    expect(isPlaybookSkill({ Metadata: '{}' })).toBe(false);
  });
});

describe('the meeting', () => {
  it("shows the playbook's quick actions first among the chat buttons, once each", () => {
    expect(
      quickActionButtons({
        QuickActions: [
          { Label: 'Scope', Prompt: 'Add up.' },
          { Label: 'Scope', Prompt: 'Again.' },
          { Label: '', Prompt: 'x' },
        ],
      }),
    ).toEqual({ '0#Scope': 'Add up.' });
    expect(quickActionButtons(null)).toEqual({});
  });

  it('says what a playbook may cost, or that it turns no model calls on', () => {
    expect(costSentence({ lines: [{}], perHourAtMost: 0.0784, perMeetingAtMost: 0.1951, unpriced: [] })).toMatch(
      /^Estimated cost: up to about \$0\.08 per meeting-hour, and at most about \$0\.20 for a meeting/,
    );
    expect(costSentence({ lines: [] })).toMatch(/no model calls/);
    expect(costSentence({ lines: [{}], perHourAtMost: 0, perMeetingAtMost: 0, unpriced: ['Live guidance'] })).toMatch(
      /Live guidance not priced/,
    );
  });
});

describe("the Virtual Participant form's choice", () => {
  it('sends nothing for the default, and the id or none otherwise', () => {
    expect(playbookCreateInput('default')).toEqual({});
    expect(playbookCreateInput('none')).toEqual({ playbook: 'none' });
    expect(playbookCreateInput('sprint-planning')).toEqual({ playbook: 'sprint-planning' });
  });

  it('sends an edit only when it changed, and null to go back to the default', () => {
    const vp = { playbook: 'retrospective' };
    expect(playbookChoiceOf(vp)).toBe('retrospective');
    expect(playbookEditInput('retrospective', vp)).toEqual({});
    expect(playbookEditInput('default', vp)).toEqual({ playbook: null });
    expect(playbookEditInput('none', {})).toEqual({ playbook: 'none' });
  });

  it("names the profile's default in the select, and the stored choice on the VP's page", () => {
    const options = {
      ProfileDefault: 'retrospective',
      Playbooks: [{ SkillId: 'retrospective', DisplayName: 'Retrospective' }],
    };
    expect(playbookSelectOptions(options).map((o) => o.label)).toEqual([
      "The profile's default (Retrospective)",
      'No playbook',
      'Retrospective',
    ]);
    expect(describePlaybookChoice({})).toBe("The configuration profile's default");
    expect(describePlaybookChoice({ playbook: 'none' })).toBe('No playbook');
  });
});
