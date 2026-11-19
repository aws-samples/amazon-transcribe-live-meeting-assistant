/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Export and import of the MCP server library, for Admin users. The export is a
// download of what the stack returns, which holds no credentials; the import
// sends a chosen file and reports what it did with each server.

import React, { useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Alert, Box, Button, SpaceBetween } from '@cloudscape-design/components';

import {
  exportedLibrary,
  exportLibraryQuery,
  importLibraryMutation,
  libraryFileName,
  parseLibraryFile,
  serversNeedingCredentials,
} from './mcp-library';

const client = generateClient();
const logger = new ConsoleLogger('McpLibraryActions');

const download = (text, name) => {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
};

const ACTION_TEXT = {
  created: 'created',
  updated: 'updated',
  skipped: 'skipped',
  failed: 'failed',
};

export const ImportResult = ({ result }) => {
  const servers = result?.Servers || [];
  const waiting = servers.filter((server) => server.CredentialsRequired);
  return (
    <Alert
      type={result.Success ? 'success' : 'error'}
      header={result.Success ? 'MCP server library imported' : 'The import did not complete'}
    >
      <SpaceBetween size="xs">
        {result.Message && <Box>{result.Message}</Box>}
        <ul style={{ margin: 0 }}>
          {servers.map((server) => (
            <li key={`${server.Name}-${server.ServerId}`}>
              {`${server.Name}: ${ACTION_TEXT[server.Action] || server.Action}`}
              {server.CredentialsRequired ? ', needs credentials' : ''}
              {server.Message ? ` (${server.Message})` : ''}
            </li>
          ))}
        </ul>
        {waiting.length > 0 && (
          <Box>
            {`${waiting.length} imported ${waiting.length === 1 ? 'server needs' : 'servers need'} credentials. `}
            The meeting assistant does not use a server until its credentials are entered with Enter credentials on its
            card below.
          </Box>
        )}
      </SpaceBetween>
    </Alert>
  );
};

ImportResult.propTypes = {
  result: PropTypes.shape({
    Success: PropTypes.bool,
    Message: PropTypes.string,
    Servers: PropTypes.arrayOf(PropTypes.shape({})),
  }).isRequired,
};

const McpLibraryActions = ({ onImported, stackName = '' }) => {
  const fileInput = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const exportLibrary = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await client.graphql({ query: exportLibraryQuery });
      const document = exportedLibrary(response.data.exportMCPServerLibrary);
      download(`${JSON.stringify(document, null, 2)}\n`, libraryFileName(stackName));
    } catch (err) {
      logger.error('Export of the MCP server library failed', err);
      setError(err.message || 'The export failed');
    } finally {
      setBusy(false);
    }
  };

  const importFile = async (file) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const document = parseLibraryFile(await file.text());
      logger.info(
        `Importing ${document.servers.length} MCP servers, ${serversNeedingCredentials(document)} with credentials`,
      );
      const response = await client.graphql({
        query: importLibraryMutation,
        variables: { library: JSON.stringify(document) },
      });
      setResult(response.data.importMCPServerLibrary);
      onImported();
    } catch (err) {
      logger.error('Import of the MCP server library failed', err);
      setError(err.message || (err.errors && err.errors[0]?.message) || 'The import failed');
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  return (
    <SpaceBetween size="s">
      <SpaceBetween direction="horizontal" size="xs">
        <Button iconName="download" onClick={exportLibrary} loading={busy} ariaLabel="Export the MCP server library">
          Export
        </Button>
        <Button
          iconName="upload"
          onClick={() => fileInput.current?.click()}
          disabled={busy}
          ariaLabel="Import an MCP server library"
        >
          Import
        </Button>
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          style={{ display: 'none' }}
          data-testid="mcp-library-file"
          onChange={(event) => importFile(event.target.files?.[0])}
        />
      </SpaceBetween>
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="MCP server library">
          {error}
        </Alert>
      )}
      {result && <ImportResult result={result} />}
    </SpaceBetween>
  );
};

McpLibraryActions.propTypes = {
  onImported: PropTypes.func.isRequired,
  stackName: PropTypes.string,
};

export default McpLibraryActions;
