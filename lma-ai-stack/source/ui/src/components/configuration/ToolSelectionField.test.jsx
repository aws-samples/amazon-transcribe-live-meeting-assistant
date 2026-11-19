/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Meeting Assistant › Tool selection on the Configuration page, against the
 * section schema as the configuration layer ships it. What is asserted: each
 * installed server is listed with its three flags; a change stores only the
 * flag that differs from the default, and a server left with none is removed;
 * a server the profile names that is not installed is shown so it can be
 * removed; and a read-only profile offers no change.
 */
import React from 'react';
import { readFileSync } from 'fs';
import { join } from 'path';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SchemaNode, SectionProvider } from './fields';
import ToolSelectionField, { withFlag } from './ToolSelectionField';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const SCHEMA = JSON.parse(
  readFileSync(
    join(import.meta.dirname, '../../../../lambda_layers/configuration_layer/lma_config/schemas/meeting_assist.json'),
    'utf8',
  ),
);
const TOOL_SCHEMA = SCHEMA.properties.toolSelection;
const DEFAULTS = { toolSelection: { mcpServers: {}, skills: {} } };
const SERVERS = [
  { ServerId: 'alpha', Name: 'Alpha', Status: 'ACTIVE' },
  { ServerId: 'beta', Name: 'Beta', Status: 'ACTIVE', CredentialsRequired: true },
];

const renderField = ({ values = DEFAULTS, readOnly = false, servers = SERVERS } = {}) => {
  const onChange = vi.fn();
  const loadServers = vi.fn(() => Promise.resolve(servers));
  // eslint-disable-next-line react/prop-types
  const Field = ({ schema, path, label }) => (
    <ToolSelectionField schema={schema} path={path} label={label} loadServers={loadServers} />
  );
  render(
    <SectionProvider
      values={values}
      defaults={DEFAULTS}
      problems={[]}
      readOnly={readOnly}
      widgets={{ toolSelection: Field }}
      onChange={onChange}
    >
      <SchemaNode schema={TOOL_SCHEMA} path={['toolSelection']} label="Tool selection" />
    </SectionProvider>,
  );
  return { onChange, loadServers };
};

const stored = (onChange) => onChange.mock.calls.at(-1)[0].toolSelection.mcpServers;

describe('Tool selection', () => {
  it('is the control the schema names for the field', () => {
    expect(TOOL_SCHEMA.widget).toBe('toolSelection');
    expect(Object.keys(TOOL_SCHEMA.properties)).toEqual(['mcpServers', 'skills']);
    expect(Object.keys(TOOL_SCHEMA.properties.mcpServers.additionalProperties.properties)).toEqual([
      'enabled',
      'inMeetingRequests',
      'alwaysOn',
    ]);
  });

  it('lists every installed server with its flags at their defaults', async () => {
    renderField();
    expect(await screen.findByText('Alpha')).toBeTruthy();
    expect(screen.getByText('Needs credentials')).toBeTruthy();
    expect(screen.getByLabelText('Enabled: Alpha').checked).toBe(true);
    expect(screen.getByLabelText('Always on: Alpha').checked).toBe(false);
  });

  it('turning a server off stores only enabled false for it', async () => {
    const { onChange } = renderField();
    fireEvent.click(await screen.findByLabelText('Enabled: Beta'));
    expect(stored(onChange)).toEqual({ beta: { enabled: false } });
  });

  it('turning a server back on removes its entry, so it behaves as an unlisted one', async () => {
    const { onChange } = renderField({
      values: { toolSelection: { mcpServers: { alpha: { enabled: false } }, skills: {} } },
    });
    fireEvent.click(await screen.findByLabelText('Enabled: Alpha'));
    expect(stored(onChange)).toEqual({});
  });

  it('a server the profile names that is not installed is listed and can be removed', async () => {
    const { onChange } = renderField({
      values: { toolSelection: { mcpServers: { gone: { inMeetingRequests: true } }, skills: {} } },
    });
    expect(await screen.findByText('Not installed')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Remove gone'));
    expect(stored(onChange)).toEqual({});
  });

  it('a read-only profile offers no change', async () => {
    renderField({ readOnly: true });
    await screen.findByText('Alpha');
    expect(screen.getByLabelText('Enabled: Alpha').disabled).toBe(true);
  });

  it('says so when no server is installed', async () => {
    renderField({ servers: [] });
    await waitFor(() => expect(screen.getByText(/No MCP servers are installed/)).toBeTruthy());
  });
});

describe('withFlag', () => {
  it('sets, clears, and drops a server left with no flag', () => {
    expect(withFlag({}, 'a', 'inMeetingRequests', false)).toEqual({ a: { inMeetingRequests: false } });
    expect(withFlag({ a: { inMeetingRequests: false, alwaysOn: true } }, 'a', 'alwaysOn', undefined)).toEqual({
      a: { inMeetingRequests: false },
    });
    expect(withFlag({ a: { enabled: false } }, 'a', 'enabled', undefined)).toEqual({});
  });
});
