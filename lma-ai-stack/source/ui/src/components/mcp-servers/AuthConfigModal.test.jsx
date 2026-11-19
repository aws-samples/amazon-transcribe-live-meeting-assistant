/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The credentials form of a server imported from an MCP server library opens on
 * the type the server needs, with the names and OAuth settings the library kept,
 * and every credential empty.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import AuthConfigModal from './AuthConfigModal';
import { importedSettings } from './MCPServersContent';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: true }) }));

const HTTP_SERVER = { id: 'https://mcp.example.com/mcp', name: 'Team tools', packageType: 'streamable-http' };

const open = (initialSettings) =>
  render(
    <AuthConfigModal
      visible
      onDismiss={vi.fn()}
      onSubmit={vi.fn()}
      server={HTTP_SERVER}
      initialSettings={initialSettings}
    />,
  );

describe('the credentials form of an imported server', () => {
  it('opens on custom headers with the header names and empty values', () => {
    open({ authType: 'custom_headers', headers: { 'X-API-Key': '', 'X-Team': '' } });
    expect(screen.getAllByText('Custom Headers (JSON)').length).toBeGreaterThan(0);
    const text = screen.getAllByRole('textbox').map((box) => box.value);
    expect(text).toContain(JSON.stringify({ 'X-API-Key': '', 'X-Team': '' }, null, 2));
  });

  it('opens on client credentials with the client settings and an empty secret', () => {
    open({
      authType: 'oauth_client_credentials',
      oauth: { clientId: 'client-id-123', tokenUrl: 'https://auth.example.com/token', scopes: ['read', 'write'] },
    });
    const values = screen.getAllByRole('textbox').map((box) => box.value);
    expect(values).toContain('client-id-123');
    expect(values).toContain('https://auth.example.com/token');
    expect(values).toContain('read write');
  });
});

describe('importedSettings', () => {
  it('reads the settings the list returns, or falls back to the required type', () => {
    expect(importedSettings({ ImportedAuthSettings: '{"authType":"bearer","token":""}' })).toEqual({
      authType: 'bearer',
      token: '',
    });
    expect(importedSettings({ ImportedAuthSettings: 'nope', RequiredAuthType: 'env_vars' })).toEqual({
      authType: 'env_vars',
    });
    expect(importedSettings(null)).toBeNull();
  });
});
