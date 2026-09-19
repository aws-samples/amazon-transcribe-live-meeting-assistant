/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A custom server is identified by a slug derived from the free-text name the
 * user types. The install mutation and the OAuth flow must use the *same* slug:
 * the OAuth callback keys its DynamoDB row on the identifier it is given, so two
 * different values would leave the credential on a row the installed server never
 * reads. The API also accepts only a restricted identifier shape, so the slug has
 * to satisfy that for any name a user can type.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const { graphql, authModalProps } = vi.hoisted(() => ({
  graphql: vi.fn(),
  authModalProps: { current: null },
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

// Capture what the OAuth modal is handed, and give it a way to complete.
vi.mock('./AuthConfigModal', () => ({
  default: (props) => {
    authModalProps.current = props;
    return props.visible ? <div>auth config modal</div> : null;
  },
}));

// eslint-disable-next-line import/first
import CustomServersTab, { customServerId } from './CustomServersTab';

// The identifier shape the MCPServerManager and OAuthManager functions accept.
const ACCEPTED_SERVER_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*(\/[A-Za-z0-9][A-Za-z0-9._-]*)?$/;

const fillForm = (name, url) => {
  const [nameInput, urlInput] = screen.getAllByRole('textbox');
  fireEvent.change(nameInput, { target: { value: name } });
  fireEvent.change(urlInput, { target: { value: url } });
};

beforeEach(() => {
  graphql.mockReset();
  graphql.mockResolvedValue({ data: { installMCPServer: { Success: true, ServerId: 'x' } } });
  authModalProps.current = null;
});

describe('the identifier derived from a server name', () => {
  it.each([
    ['Salesforce MCP', 'custom/salesforce-mcp'],
    ['My Company Server', 'custom/my-company-server'],
    ["Acme's Server", 'custom/acme-s-server'],
    ['Ünicode Ünd 123', 'custom/nicode--nd-123'],
    ['  Padded Name  ', 'custom/padded-name'],
    ['???', 'custom/server'],
  ])('turns %s into an identifier the API accepts', (name, expected) => {
    expect(customServerId(name)).toBe(expected);
    expect(customServerId(name)).toMatch(ACCEPTED_SERVER_ID);
  });
});

describe('a custom server with a multi-word name', () => {
  it('installs under the derived identifier', async () => {
    render(<CustomServersTab />);
    fillForm('Salesforce MCP', 'https://example.test/mcp');

    fireEvent.click(screen.getByText('Add Server'));

    await waitFor(() => expect(graphql).toHaveBeenCalled());
    const { input } = graphql.mock.calls[0][0].variables;
    expect(input.ServerId).toBe('custom/salesforce-mcp');
    expect(input.Name).toBe('Salesforce MCP');
  });

  it('hands the OAuth flow the same identifier it installs under', async () => {
    render(<CustomServersTab />);
    fillForm('Salesforce MCP', 'https://example.test/mcp');
    fireEvent.click(screen.getByLabelText(/requires authentication/i));

    fireEvent.click(screen.getByText('Add Server'));

    await waitFor(() => expect(authModalProps.current.visible).toBe(true));
    const oauthServerId = authModalProps.current.server.id;
    expect(oauthServerId).toBe('custom/salesforce-mcp');
    expect(oauthServerId).toMatch(ACCEPTED_SERVER_ID);

    // Completing the modal installs; the two identifiers must agree.
    await authModalProps.current.onSubmit({ authType: 'bearer', token: 't' });
    await waitFor(() => expect(graphql).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0].variables.input.ServerId).toBe(oauthServerId);
  });
});
