/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Skills page and its helpers. What is asserted: the list is shown to every
 * signed-in user and only an Admin is offered changes; the editor sends the
 * SkillInput the stack takes and refuses a form the stack would; a skill file is
 * sent as text or, for a zip, as base64, and the result lists what the stack
 * left out; a library file is checked, sent in parts when large, and its result
 * shown; and an export is downloaded as the stack returns it.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  exportedFile,
  formProblems,
  importVariables,
  libraryFileName,
  libraryParts,
  MAX_FILE_BYTES,
  parseLibraryFile,
  skillForm,
  skillInput,
  toBase64,
} from './skills-api';

const { graphql, groups } = vi.hoisted(() => ({ graphql: vi.fn(), groups: { isAdmin: true } }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    info() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: groups.isAdmin, isLoading: false }) }));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: { StackName: 'LMA' } }) }));

// eslint-disable-next-line import/first
import SkillsPage from './SkillsPage';

const SKILL = {
  SkillId: 'launch-review',
  Name: 'launch-review',
  DisplayName: 'Launch review',
  Description: 'Review a launch plan.',
  Files: [{ Path: 'reference/checklist.md', Size: 10 }],
  Metadata: JSON.stringify({ trigger: 'review the launch' }),
  SourceFormat: 'amazon-quick',
  UpdatedAt: '2026-11-19T00:00:00Z',
  UpdatedBy: 'admin@example.com',
};
const LIBRARY = { kind: 'lma-skill-library', version: 1, exportedAt: 'x', skills: [{ name: 'a' }] };

const answers = {};
const respond = (field, value) => {
  answers[field] = value;
};

beforeEach(() => {
  groups.isAdmin = true;
  Object.keys(answers).forEach((k) => delete answers[k]);
  graphql.mockReset();
  graphql.mockImplementation(({ query }) => {
    const field = Object.keys(answers).find((name) => query.includes(`${name}(`) || query.includes(`${name} {`));
    if (!field && query.includes('listSkills')) return Promise.resolve({ data: { listSkills: [SKILL] } });
    return Promise.resolve({ data: { [field]: answers[field] } });
  });
  global.URL.createObjectURL = vi.fn(() => 'blob:x');
  global.URL.revokeObjectURL = vi.fn();
});

afterEach(() => vi.restoreAllMocks());

const calls = (field) => graphql.mock.calls.map(([args]) => args).filter((args) => args.query.includes(field));

const fileOf = (name, text) => ({
  name,
  text: () => Promise.resolve(text),
  arrayBuffer: async () => new TextEncoder().encode(text).buffer,
});

describe('the Skills page', () => {
  it('lists the skills with their trigger and source', async () => {
    render(<SkillsPage />);
    expect(await screen.findByText('Launch review (launch-review)')).toBeTruthy();
    expect(screen.getByText('review the launch')).toBeTruthy();
    expect(screen.getByText('Amazon Quick')).toBeTruthy();
  });

  it('offers a user outside the Admin group no change, only export', async () => {
    groups.isAdmin = false;
    render(<SkillsPage />);
    await screen.findByText('Launch review (launch-review)');
    expect(screen.queryByText('Create skill')).toBeNull();
    expect(screen.queryByText('Edit')).toBeNull();
    expect(screen.getByText('Export skill')).toBeTruthy();
    expect(screen.getByText(/Skills are managed by administrators/)).toBeTruthy();
  });

  it('creates a skill from the editor with the input the stack takes', async () => {
    respond('createSkill', { SkillId: 'incident-notes', Success: true });
    render(<SkillsPage />);
    fireEvent.click(await screen.findByText('Create skill'));
    const dialog = screen.getByRole('dialog', { name: 'New skill' });
    const typeInto = (label, value) => {
      const element = within(dialog).getByLabelText(label);
      fireEvent.change(element.querySelector('textarea, input') || element, { target: { value } });
    };
    typeInto('Name', 'incident-notes');
    typeInto('Description', 'Take notes during an incident.');
    typeInto('Instructions', '# Incident notes');
    fireEvent.click(within(dialog).getByText('Save'));
    await waitFor(() => expect(calls('createSkill')).toHaveLength(1));
    expect(calls('createSkill')[0].variables.input).toEqual({
      Name: 'incident-notes',
      DisplayName: null,
      Description: 'Take notes during an incident.',
      Instructions: '# Incident notes',
      Files: [],
      Metadata: '{}',
    });
  });

  it('imports a zip as base64 and lists the files the stack left out', async () => {
    respond('importSkill', {
      Success: true,
      Message: 'Skill s created; 1 file left out',
      Skipped: [{ Path: 'scripts/run.py', Reason: 'only text and Markdown reference files are kept' }],
      Notes: [],
    });
    render(<SkillsPage />);
    await screen.findByText('Launch review (launch-review)');
    fireEvent.change(screen.getByTestId('skill-file'), { target: { files: [fileOf('s.zip', 'PK')] } });
    expect(await screen.findByText(/scripts\/run.py: only text and Markdown/)).toBeTruthy();
    const { variables } = calls('importSkill')[0];
    expect(variables).toEqual({ fileName: 's.zip', content: btoa('PK'), encoding: 'base64', overwrite: false });
  });

  it('shows the stack refusing a skill file', async () => {
    respond('importSkill', { Success: false, Message: 'The file has no front matter', Skipped: [], Notes: [] });
    render(<SkillsPage />);
    await screen.findByText('Launch review (launch-review)');
    fireEvent.change(screen.getByTestId('skill-file'), { target: { files: [fileOf('a.md', 'x')] } });
    expect(await screen.findByText('The skill was not imported')).toBeTruthy();
    expect(screen.getByText('The file has no front matter')).toBeTruthy();
  });

  it('imports a library file and reports each skill', async () => {
    respond('importSkillLibrary', {
      Success: true,
      Message: '1 of 1 skills processed',
      Skills: [{ Name: 'a', SkillId: 'a', Action: 'created', Message: '' }],
    });
    render(<SkillsPage />);
    await screen.findByText('Launch review (launch-review)');
    fireEvent.change(screen.getByTestId('skill-library-file'), {
      target: { files: [fileOf('skills.json', JSON.stringify(LIBRARY))] },
    });
    expect(await screen.findByText('a: created')).toBeTruthy();
    expect(JSON.parse(calls('importSkillLibrary')[0].variables.library)).toEqual(LIBRARY);
  });

  it('refuses a file that is not a skill library before sending it', async () => {
    render(<SkillsPage />);
    await screen.findByText('Launch review (launch-review)');
    fireEvent.change(screen.getByTestId('skill-library-file'), {
      target: { files: [fileOf('x.json', JSON.stringify({ kind: 'lma-mcp-server-library', version: 1 }))] },
    });
    expect(await screen.findByText('The file is not an exported skill library.')).toBeTruthy();
    expect(calls('importSkillLibrary')).toHaveLength(0);
  });
});

describe('the skill helpers', () => {
  it('round-trips a stored skill through the editor form', () => {
    const stored = {
      ...SKILL,
      Instructions: 'Do it.',
      Files: [{ Path: 'reference/checklist.md', Content: '- Pricing', Size: 9 }],
    };
    expect(skillInput(skillForm(stored))).toEqual({
      Name: 'launch-review',
      DisplayName: 'Launch review',
      Description: 'Review a launch plan.',
      Instructions: 'Do it.',
      Files: [{ Path: 'reference/checklist.md', Content: '- Pricing' }],
      Metadata: JSON.stringify({ trigger: 'review the launch' }),
    });
  });

  it('names what the stack would refuse in a form', () => {
    const good = { name: 'a-b', description: 'd', instructions: 'i', files: [] };
    expect(formProblems(good)).toEqual([]);
    expect(formProblems({ ...good, name: 'A B' })[0]).toMatch(/lowercase/);
    expect(formProblems({ ...good, files: [{ path: 'a.md' }, { path: 'a.md' }] })).toContain(
      'Two reference files have the same path.',
    );
    expect(formProblems({ ...good, files: [{ path: 'big.md', content: 'x'.repeat(MAX_FILE_BYTES + 1) }] })[0]).toMatch(
      /larger than/,
    );
  });

  it('sends Markdown as text and a zip as base64', async () => {
    expect(await importVariables(fileOf('SKILL.md', '---'), true)).toEqual({
      fileName: 'SKILL.md',
      content: '---',
      encoding: 'utf-8',
      overwrite: true,
    });
    expect(toBase64(new TextEncoder().encode('hello').buffer)).toBe(btoa('hello'));
  });

  it('reads an exported zip back as bytes', async () => {
    const file = exportedFile(JSON.stringify({ fileName: 'a.zip', encoding: 'base64', content: btoa('PKzip') }));
    expect(file.name).toBe('a.zip');
    expect(Array.from(new Uint8Array(await file.blob.arrayBuffer()))).toEqual(
      Array.from(new TextEncoder().encode('PKzip')),
    );
  });

  it('checks a library file and splits a large one into parts of whole skills', () => {
    expect(parseLibraryFile(JSON.stringify(LIBRARY))).toEqual(LIBRARY);
    expect(() => parseLibraryFile('nope')).toThrow(/not valid JSON/);
    expect(() => parseLibraryFile(JSON.stringify({ ...LIBRARY, version: 2 }))).toThrow(/format version 2/);
    const skills = [0, 1, 2, 3].map((n) => ({ name: `s${n}`, instructions: 'x'.repeat(150) }));
    const { parts, duplicates } = libraryParts({ ...LIBRARY, skills: [...skills, { name: 's1' }] }, 400);
    expect(duplicates).toEqual(['s1']);
    expect(parts.length).toBeGreaterThan(1);
    expect(parts.flatMap((part) => part.skills.map((s) => s.name))).toEqual(['s0', 's1', 's2', 's3']);
    parts.forEach((part) => expect(part.kind).toBe('lma-skill-library'));
  });

  it('names an export for the stack and the day', () => {
    expect(libraryFileName('LMA', new Date('2026-11-19T10:00:00Z'))).toBe('LMA-skills-2026-11-19.json');
  });
});
