/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Meeting Assistant › Meeting playbooks › Default playbook on the Configuration
 * page, against the section schema as the configuration layer ships it: it
 * lists the skills that are playbooks and no other, stores the chosen skill id
 * or '' for none, and shows a stored id that is no longer a playbook so it can
 * be changed. See docs/meeting-playbooks.md.
 */
import React from 'react';
import { readFileSync } from 'fs';
import { join } from 'path';
import { render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import { SchemaNode, SectionProvider } from './fields';
import PlaybookSelectField from './PlaybookSelectField';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const SCHEMA = JSON.parse(
  readFileSync(
    join(import.meta.dirname, '../../../../lambda_layers/configuration_layer/lma_config/schemas/meeting_assist.json'),
    'utf8',
  ),
);
const PLAYBOOKS_SCHEMA = SCHEMA.properties.playbooks;
const DEFAULTS = { playbooks: { defaultPlaybook: '' } };
const SKILLS = [
  { SkillId: 'sprint-planning', Name: 'sprint-planning', DisplayName: 'Sprint planning' },
  { SkillId: 'retrospective', Name: 'retrospective', DisplayName: 'Retrospective' },
];

const renderField = (values = DEFAULTS) => {
  const onChange = vi.fn();
  const loadPlaybooks = vi.fn(() => Promise.resolve(SKILLS));
  // eslint-disable-next-line react/prop-types
  const Field = ({ schema, path, label }) => (
    <PlaybookSelectField schema={schema} path={path} label={label} loadPlaybooks={loadPlaybooks} />
  );
  render(
    <SectionProvider
      values={values}
      defaults={DEFAULTS}
      problems={[]}
      readOnly={false}
      widgets={{ playbookSelect: Field }}
      onChange={onChange}
    >
      <SchemaNode schema={PLAYBOOKS_SCHEMA} path={['playbooks']} label="Meeting playbooks" />
    </SectionProvider>,
  );
  return { onChange, loadPlaybooks };
};

describe('Default playbook', () => {
  it('is the control the schema names, empty by default', () => {
    expect(PLAYBOOKS_SCHEMA.properties.defaultPlaybook.widget).toBe('playbookSelect');
  });

  it('lists the playbooks and stores the chosen skill id', async () => {
    const { onChange, loadPlaybooks } = renderField();
    await waitFor(() => expect(loadPlaybooks).toHaveBeenCalled());
    const select = createWrapper(document.body).findSelect();
    select.openDropdown();
    const labels = select
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent);
    expect(labels.join(' | ')).toMatch(/None.*Sprint planning.*Retrospective/);
    select.selectOptionByValue('retrospective');
    expect(onChange.mock.calls.at(-1)[0].playbooks.defaultPlaybook).toBe('retrospective');
  });

  it('shows a stored id that is no longer a playbook', async () => {
    renderField({ playbooks: { defaultPlaybook: 'gone-playbook' } });
    expect(await screen.findByText(/gone-playbook \(not a playbook now/)).toBeInTheDocument();
  });
});
