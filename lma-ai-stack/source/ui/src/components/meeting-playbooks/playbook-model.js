/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Meeting playbooks on the pages: the names a playbook may use, the editor's
 * form and its checks, what applying one does, and its cost in words.
 *
 * A playbook is a skill whose front matter has a `playbook` section, kept in the
 * skill's metadata. The rules here mirror lma_playbooks/rules.py, which decides;
 * the page checks first so an Admin sees every problem before saving. See
 * docs/meeting-playbooks.md.
 */
import { TEMPLATES, LIMITS as WHITEBOARD_LIMITS } from '../meeting-whiteboards/whiteboard-model';

export const PLAYBOOK_KEY = 'playbook';
export const NO_PLAYBOOK = 'none';

/** The live guidance checks a playbook may turn on, as live guidance names them. */
export const GUIDANCE_CHECKS = [
  { value: 'ask_question', label: 'Questions to ask', description: 'A must or should checklist question still open.' },
  { value: 'uncovered_topic', label: 'Topics not yet covered', description: 'An agenda item nobody has reached.' },
  { value: 'clarify', label: 'Things to clarify', description: 'A vague number, date or commitment.' },
  { value: 'risk_probe', label: 'Risks to probe', description: 'A risk raised and not explored.' },
  { value: 'action_missing_owner', label: 'Actions without an owner', description: 'An action nobody took.' },
  { value: 'action_missing_due_date', label: 'Actions without a due date', description: 'An action with no date.' },
  { value: 'time_check', label: 'Time checks', description: 'Time nearly up with must items open.' },
  { value: 'fact_check', label: 'Fact checks', description: 'A claim the sources contradict.' },
  { value: 'lookup', label: 'Lookups', description: 'A term or product the meeting seems not to know.' },
];
export const RESEARCH_CHECKS = { fact_check: 'factChecksEnabled', lookup: 'lookupsEnabled' };

export const ASSERTIVENESS = [
  { value: 'quiet', label: 'Quiet', description: 'Only what the assistant is most sure of (confidence 0.85).' },
  { value: 'balanced', label: 'Balanced', description: 'The usual (confidence 0.75).' },
  { value: 'assertive', label: 'Assertive', description: 'More suggestions, some less certain (confidence 0.65).' },
];

export const LIMITS = {
  whiteboards: 5,
  quickActions: 6,
  quickActionLabel: 40,
  quickActionPrompt: 1000,
  suggestPhrases: 20,
  suggestPhrase: 60,
  whiteboardTitle: WHITEBOARD_LIMITS.title,
  whiteboardInstructions: WHITEBOARD_LIMITS.instructions,
  minItems: 1,
  maxItems: 20,
};

const TEMPLATE_NAMES = new Set(TEMPLATES.map((t) => t.name));
const CHECK_NAMES = new Set(GUIDANCE_CHECKS.map((c) => c.value));

export const templateLabel = (name) => TEMPLATES.find((t) => t.name === name)?.label || name;
export const checkLabel = (name) => GUIDANCE_CHECKS.find((c) => c.value === name)?.label || name;

/** The skill's metadata as an object, whether the API gave it as JSON text or not. */
export const metadataOf = (raw) => {
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
};

export const isPlaybookSkill = (skill) => {
  const section = metadataOf(skill?.Metadata)[PLAYBOOK_KEY];
  return Boolean(section) && typeof section === 'object';
};

/** A phrase list typed as one line, comma-separated. */
export const phrasesFromText = (text) =>
  String(text || '')
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean);

/** A new playbook section for the editor: one whiteboard and the two action checks. */
export const emptyPlaybook = () => ({
  suggestFor: [],
  whiteboards: [{ template: 'decisions', title: '', instructions: '', assistantUpdates: true }],
  guidance: { checks: ['action_missing_owner', 'action_missing_due_date'], assertiveness: 'balanced' },
  quickActions: [],
});

/**
 * The editor's form of a stored section: every part present, so each field has
 * a value; the phrases as one comma-separated line and the item rate as text.
 */
export const playbookForm = (section) => {
  const value = section && typeof section === 'object' ? section : {};
  const guidance = value.guidance && typeof value.guidance === 'object' ? value.guidance : {};
  return {
    suggestForText: (Array.isArray(value.suggestFor) ? value.suggestFor : []).join(', '),
    whiteboards: (Array.isArray(value.whiteboards) ? value.whiteboards : []).map((b) => ({
      template: b?.template || 'decisions',
      title: b?.title || '',
      instructions: b?.instructions || '',
      assistantUpdates: b?.assistantUpdates !== false,
    })),
    guidance: {
      checks: Array.isArray(guidance.checks) ? guidance.checks : [],
      assertiveness: guidance.assertiveness || 'balanced',
      maxItemsPer10Min:
        guidance.maxItemsPer10Min === undefined || guidance.maxItemsPer10Min === null
          ? ''
          : String(guidance.maxItemsPer10Min),
    },
    quickActions: (Array.isArray(value.quickActions) ? value.quickActions : []).map((a) => ({
      label: a?.label || '',
      prompt: a?.prompt || '',
    })),
  };
};

/** The section the editor saves: trimmed, and without a guidance part that names no check. */
export const playbookSection = (form) => {
  const section = {
    suggestFor: phrasesFromText(form.suggestForText),
    whiteboards: (form.whiteboards || []).map((b) => ({
      template: b.template,
      title: (b.title || '').trim(),
      instructions: (b.instructions || '').trim(),
      assistantUpdates: b.assistantUpdates !== false,
    })),
    quickActions: (form.quickActions || []).map((a) => ({
      label: (a.label || '').trim(),
      prompt: (a.prompt || '').trim(),
    })),
  };
  const guidance = form.guidance || {};
  if ((guidance.checks || []).length) {
    const items = String(guidance.maxItemsPer10Min ?? '').trim();
    section.guidance = {
      checks: guidance.checks,
      assertiveness: guidance.assertiveness || 'balanced',
      ...(items ? { maxItemsPer10Min: Number(items) } : {}),
    };
  }
  return section;
};

/**
 * A section as meetingPlaybookOptions describes a playbook, for the editor's
 * preview: what applying it does, without the deployment's switches or prices.
 */
export const describeSection = (section, { displayName = '', description = '' } = {}) => ({
  DisplayName: displayName,
  Description: description,
  Whiteboards: (section.whiteboards || []).map((b) => ({
    template: b.template,
    templateLabel: templateLabel(b.template),
    title: b.title || templateLabel(b.template),
    assistantUpdates: b.assistantUpdates !== false,
  })),
  Guidance: section.guidance
    ? {
        checks: (section.guidance.checks || []).map((c) => ({ check: c, label: checkLabel(c), available: true })),
        assertiveness: section.guidance.assertiveness,
        maxItemsPer10Min: section.guidance.maxItemsPer10Min,
      }
    : null,
  QuickActions: section.quickActions || [],
  Notes: [],
});

/** Every problem with a section, in the server's words where it has them; [] when it may be saved. */
export const playbookProblems = (section) => {
  const problems = [];
  const boards = section.whiteboards || [];
  if (boards.length > LIMITS.whiteboards)
    problems.push(`A playbook creates at most ${LIMITS.whiteboards} whiteboards.`);
  boards.forEach((b, i) => {
    const where = `Whiteboard ${i + 1}`;
    if (!TEMPLATE_NAMES.has(b.template)) problems.push(`${where}: “${b.template}” is not a whiteboard template.`);
    if ((b.title || '').length > LIMITS.whiteboardTitle)
      problems.push(`${where}: the title is longer than ${LIMITS.whiteboardTitle} characters.`);
    if ((b.instructions || '').length > LIMITS.whiteboardInstructions)
      problems.push(`${where}: the instructions are longer than ${LIMITS.whiteboardInstructions} characters.`);
  });
  const { guidance } = section;
  if (guidance) {
    (guidance.checks || []).forEach((c) => {
      if (!CHECK_NAMES.has(c)) problems.push(`“${c}” is not a live guidance check.`);
    });
    if (!ASSERTIVENESS.some((a) => a.value === guidance.assertiveness))
      problems.push('Choose how assertive the guidance is.');
    const items = guidance.maxItemsPer10Min;
    if (
      items !== undefined &&
      (!Number.isInteger(Number(items)) || Number(items) < LIMITS.minItems || Number(items) > LIMITS.maxItems)
    )
      problems.push(`Items per 10 minutes must be a whole number from ${LIMITS.minItems} to ${LIMITS.maxItems}.`);
  }
  const actions = section.quickActions || [];
  if (actions.length > LIMITS.quickActions)
    problems.push(`A playbook has at most ${LIMITS.quickActions} quick actions.`);
  actions.forEach((a, i) => {
    const where = `Quick action ${i + 1}`;
    if (!a.label) problems.push(`${where} needs a label.`);
    if (!a.prompt) problems.push(`${where} needs a prompt.`);
    if (a.label.length > LIMITS.quickActionLabel)
      problems.push(`${where}: the label is longer than ${LIMITS.quickActionLabel} characters.`);
    if (a.prompt.length > LIMITS.quickActionPrompt)
      problems.push(`${where}: the prompt is longer than ${LIMITS.quickActionPrompt} characters.`);
  });
  const phrases = section.suggestFor || [];
  if (phrases.length > LIMITS.suggestPhrases)
    problems.push(`At most ${LIMITS.suggestPhrases} phrases suggest the playbook.`);
  phrases.forEach((p) => {
    if (p.length > LIMITS.suggestPhrase)
      problems.push(`“${p.slice(0, 20)}…” is longer than ${LIMITS.suggestPhrase} characters.`);
  });
  if (!boards.length && !guidance && !actions.length)
    problems.push('A playbook makes a whiteboard, turns on a guidance check, or adds a quick action.');
  return problems;
};

// ── cost and effect ─────────────────────────────────────────────────────────

const money = (value) => {
  if (value === null || value === undefined) return null;
  if (value === 0) return '$0';
  if (value < 0.01) return 'under $0.01';
  return `about $${value.toFixed(2)}`;
};

/** A playbook's estimate in one sentence, or null when it turns on no model calls. */
export const costSentence = (estimate) => {
  if (!estimate || !(estimate.lines || []).length)
    return 'Turns on no model calls; making the whiteboards costs nothing.';
  const hour = money(estimate.perHourAtMost);
  const meeting = money(estimate.perMeetingAtMost);
  const unpriced = (estimate.unpriced || []).length
    ? ` ${estimate.unpriced.join(' and ')} not priced: the price table does not list the model.`
    : '';
  return `Estimated cost: up to ${hour} per meeting-hour, and at most ${meeting} for a meeting, at this deployment's limits.${unpriced}`;
};

/** What happens, and when, once a playbook is applied to a meeting in progress. */
export const TAKES_EFFECT =
  'Its whiteboards are made at once. Live guidance uses its checks from the next run, within a minute or two ' +
  'of speech. Its quick actions appear in the assistant chat at once, and the assistant follows its ' +
  'instructions from the next question.';

/** The meeting's quick actions as chat buttons ("N#LABEL": prompt), shown before the deployment's. */
export const quickActionButtons = (playbook) => {
  const buttons = {};
  (playbook?.QuickActions || []).forEach((action) => {
    const label = String(action?.Label || '').trim();
    const prompt = String(action?.Prompt || '').trim();
    if (label && prompt && !buttons[`0#${label}`]) buttons[`0#${label}`] = prompt;
  });
  return buttons;
};

/** The meeting's playbook record, as the meeting holds it. */
export const meetingPlaybook = (item) => {
  const record = item?.playbook;
  return record && typeof record === 'object' ? record : null;
};
