/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The MCP servers panel is rendered both on the configuration page and in the
 * meeting CallPanel modal. Installed servers are account-wide, so the install,
 * update and uninstall operations belong to the Admin group and the API enforces
 * that on its own. These tests pin the UI to the same rule, so a non-admin sees
 * the current state without controls that would be refused.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const INSTALLED_SERVER = {
  AccountId: '111122223333',
  ServerId: 'example-server',
  Name: 'Example Server',
  NpmPackage: 'mcp-server-example',
  PackageType: 'pypi',
  Version: '1.0.0',
  Status: 'ACTIVE',
  InstalledAt: '2025-01-01T00:00:00Z',
  UpdatedAt: '2025-01-01T00:00:00Z',
  RequiresAuth: false,
  Transport: ['stdio'],
};

// vi.mock factories are hoisted above the module scope, so anything they close
// over has to be hoisted with them.
const { graphql, userGroups } = vi.hoisted(() => ({
  graphql: vi.fn(),
  userGroups: { isAdmin: false, userGroups: [] },
}));

vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    debug() {}

    info() {}

    warn() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));

vi.mock('./PublicRegistryTab', () => ({ default: () => <div>public registry tab body</div> }));
vi.mock('./CustomServersTab', () => ({ default: () => <div>custom servers tab body</div> }));

vi.mock('../../hooks/use-user-groups', () => ({ default: () => userGroups }));

// eslint-disable-next-line import/first
import MCPServersContent from './MCPServersContent';

const renderPanel = async ({ isAdmin }) => {
  userGroups.isAdmin = isAdmin;
  userGroups.userGroups = isAdmin ? ['Admin'] : ['Users'];
  render(<MCPServersContent />);
  await waitFor(() => expect(screen.getByText('Example Server')).toBeTruthy());
};

beforeEach(() => {
  graphql.mockReset();
  graphql.mockResolvedValue({ data: { listInstalledMCPServers: [INSTALLED_SERVER] } });
  // The registry lookup is a plain fetch to a public endpoint.
  global.fetch = vi.fn().mockResolvedValue({ ok: false });
});

describe('MCP servers panel for a non-admin user', () => {
  it('lists the installed servers', async () => {
    await renderPanel({ isAdmin: false });

    expect(screen.getByText('mcp-server-example')).toBeTruthy();
  });

  it('offers no per-server update or uninstall action', async () => {
    await renderPanel({ isAdmin: false });

    expect(screen.queryByText('Uninstall')).toBeNull();
    expect(screen.queryByText('Update')).toBeNull();
  });

  it('offers no install tabs', async () => {
    await renderPanel({ isAdmin: false });

    expect(screen.queryByText('Public Registry')).toBeNull();
    expect(screen.queryByText('Custom Server')).toBeNull();
  });

  it('says who can change the installed servers', async () => {
    await renderPanel({ isAdmin: false });

    expect(screen.getByText('View only')).toBeTruthy();
  });
});

describe('MCP servers panel for an admin user', () => {
  it('offers the uninstall action', async () => {
    await renderPanel({ isAdmin: true });

    expect(screen.getByText('Uninstall')).toBeTruthy();
  });

  it('offers the install tabs', async () => {
    await renderPanel({ isAdmin: true });

    expect(screen.getByText('Public Registry')).toBeTruthy();
    expect(screen.getByText('Custom Server')).toBeTruthy();
  });

  it('does not show the view-only notice', async () => {
    await renderPanel({ isAdmin: true });

    expect(screen.queryByText('View only')).toBeNull();
  });
});

describe('the installed-server query', () => {
  it('does not ask for stored credential material', async () => {
    await renderPanel({ isAdmin: true });

    const [{ query }] = graphql.mock.calls[0];
    expect(query).toContain('listInstalledMCPServers');
    expect(query).not.toContain('AuthConfig');
  });
});
