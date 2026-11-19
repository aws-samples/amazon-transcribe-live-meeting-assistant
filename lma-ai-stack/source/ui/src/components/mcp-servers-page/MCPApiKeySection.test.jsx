/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The MCP API key's scope: chosen when the key is generated (read by default),
 * and shown with the key once it exists and in the key's one-time display.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MCPApiKeySection, { LEGACY_HINT, scopeLabel } from './MCPApiKeySection';

const { graphqlMock, state } = vi.hoisted(() => ({ graphqlMock: vi.fn(), state: { keys: [] } }));
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

beforeEach(() => {
  state.keys = [];
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query, variables }) => {
    if (query.includes('query ListMCPApiKeys')) return Promise.resolve({ data: { listMCPApiKeys: state.keys } });
    if (query.includes('mutation GenerateMCPApiKey')) {
      const scope = variables?.scope || 'read';
      state.keys = [{ keyPrefix: 'lma_abcdefgh', createdAt: '2026-11-19T10:00:00Z', enabled: true, scope }];
      return Promise.resolve({
        data: {
          generateMCPApiKey: { keyValue: 'lma_abcdefgh-rest', keyPrefix: 'lma_abcdefgh', createdAt: 'now', scope },
        },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

const generateCall = () => graphqlMock.mock.calls.find(([options]) => options.query.includes('GenerateMCPApiKey'));

describe('MCPApiKeySection key scope', () => {
  it('offers both scopes and generates a read key by default', async () => {
    render(<MCPApiKeySection />);
    expect(await screen.findByText('Read only')).toBeInTheDocument();
    expect(screen.getByText('Read and write')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Generate API Key' }));
    await waitFor(() => expect(generateCall()).toBeTruthy());
    expect(generateCall()[0].variables).toEqual({ scope: 'read' });
  });

  it('generates a read-write key when that scope is chosen, and says so', async () => {
    render(<MCPApiKeySection />);
    fireEvent.click(await screen.findByLabelText(/Read and write/));
    fireEvent.click(screen.getByRole('button', { name: 'Generate API Key' }));
    await waitFor(() => expect(generateCall()).toBeTruthy());
    expect(generateCall()[0].variables).toEqual({ scope: 'read-write' });
    expect(await screen.findByText('API Key Generated')).toBeInTheDocument();
    expect(screen.getAllByText('Read and write').length).toBeGreaterThan(0);
  });

  it('lists an existing key with its scope', async () => {
    state.keys = [{ keyPrefix: 'lma_existing', createdAt: '2026-11-01T10:00:00Z', enabled: true, scope: 'read' }];
    render(<MCPApiKeySection />);
    expect(await screen.findByText('Read only')).toBeInTheDocument();
    // The scope choice is only offered before a key exists.
    expect(screen.queryByText('Read and write')).not.toBeInTheDocument();
    expect(screen.queryByText(LEGACY_HINT)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate API Key' })).toBeDisabled();
  });

  it.each(['legacy', null])(
    'lists a key created before scopes (%s) as legacy, with how to get the new scopes',
    async (scope) => {
      state.keys = [{ keyPrefix: 'lma_existing', createdAt: '2026-11-01T10:00:00Z', enabled: true, scope }];
      render(<MCPApiKeySection />);
      expect(await screen.findByText('Legacy (original tools)')).toBeInTheDocument();
      expect(screen.getByText(LEGACY_HINT)).toBeInTheDocument();
    },
  );

  it('labels each scope', () => {
    expect(scopeLabel('read-write')).toBe('Read and write');
    expect(scopeLabel('read')).toBe('Read only');
    expect(scopeLabel('legacy')).toBe('Legacy (original tools)');
    expect(scopeLabel(undefined)).toBe('Legacy (original tools)');
  });
});
