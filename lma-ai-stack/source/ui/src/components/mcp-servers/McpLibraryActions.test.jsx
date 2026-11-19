/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The MCP server library's export and import on the MCP Servers page. The
 * export is downloaded as the stack returns it; an import file is checked
 * before it is sent, and the result says which servers need credentials.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { libraryFileName, parseLibraryFile, serversNeedingCredentials } from './mcp-library';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    info() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));

// eslint-disable-next-line import/first
import McpLibraryActions from './McpLibraryActions';

const LIBRARY = {
  kind: 'lma-mcp-server-library',
  version: 1,
  exportedAt: '2026-11-19T00:00:00Z',
  servers: [
    { name: 'Docs', auth: { authType: 'bearer', token: '' }, credentialsToReenter: ['auth.token'] },
    { name: 'Open', auth: { authType: 'none' }, credentialsToReenter: [] },
  ],
};

const fileOf = (text) => ({ text: () => Promise.resolve(text) });

describe('the library file', () => {
  it('is read only when it is a library of this format', () => {
    expect(parseLibraryFile(JSON.stringify(LIBRARY))).toEqual(LIBRARY);
    expect(() => parseLibraryFile('nope')).toThrow(/not valid JSON/);
    expect(() => parseLibraryFile(JSON.stringify({ kind: 'lma-skill-library', version: 1 }))).toThrow(
      /not an exported MCP server library/,
    );
    expect(() => parseLibraryFile(JSON.stringify({ ...LIBRARY, version: 2 }))).toThrow(/format version 2/);
  });

  it('counts the servers whose credentials will be entered again', () => {
    expect(serversNeedingCredentials(LIBRARY)).toBe(1);
  });

  it('is named for the stack and the day', () => {
    expect(libraryFileName('LMA', new Date('2026-11-19T10:00:00Z'))).toBe('LMA-mcp-servers-2026-11-19.json');
  });
});

describe('McpLibraryActions', () => {
  beforeEach(() => graphql.mockReset());

  it('downloads the export the stack returns', async () => {
    graphql.mockResolvedValue({ data: { exportMCPServerLibrary: JSON.stringify(LIBRARY) } });
    const created = vi.fn(() => 'blob:x');
    URL.createObjectURL = created;
    URL.revokeObjectURL = vi.fn();
    render(<McpLibraryActions onImported={vi.fn()} />);
    fireEvent.click(screen.getByText('Export'));
    await waitFor(() => expect(created).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0].query).toMatch(/exportMCPServerLibrary/);
  });

  it('imports a chosen file and says which servers need credentials', async () => {
    graphql.mockResolvedValue({
      data: {
        importMCPServerLibrary: {
          Success: true,
          Message: '2 of 2 servers processed',
          Servers: [
            { Name: 'Docs', ServerId: 'docs', Action: 'created', CredentialsRequired: true, Message: '' },
            { Name: 'Open', ServerId: 'open', Action: 'updated', CredentialsRequired: false, Message: '' },
          ],
        },
      },
    });
    const onImported = vi.fn();
    render(<McpLibraryActions onImported={onImported} />);
    fireEvent.change(screen.getByTestId('mcp-library-file'), {
      target: { files: [fileOf(JSON.stringify(LIBRARY))] },
    });
    expect(await screen.findByText('Docs: created, needs credentials')).toBeTruthy();
    expect(screen.getByText('Open: updated')).toBeTruthy();
    expect(JSON.parse(graphql.mock.calls[0][0].variables.library)).toEqual(LIBRARY);
    expect(onImported).toHaveBeenCalled();
  });

  it('refuses a file that is not a library without calling the stack', async () => {
    render(<McpLibraryActions onImported={vi.fn()} />);
    fireEvent.change(screen.getByTestId('mcp-library-file'), { target: { files: [fileOf('{"kind":"x"}')] } });
    expect(await screen.findByText(/not an exported MCP server library/)).toBeTruthy();
    expect(graphql).not.toHaveBeenCalled();
  });
});
