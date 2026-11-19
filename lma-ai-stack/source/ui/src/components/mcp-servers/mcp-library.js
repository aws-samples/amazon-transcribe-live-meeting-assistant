/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The MCP server library: a deployment's installed servers as a JSON document
// that another deployment can import. The stack builds the export
// (exportMCPServerLibrary) and it never holds a credential: API keys, bearer
// tokens, OAuth client secrets and tokens are empty placeholders, listed in each
// server's credentialsToReenter. An import creates or updates servers by name
// and stores no credential; a server that needs one is marked
// CredentialsRequired until an Admin enters it.

export const LIBRARY_KIND = 'lma-mcp-server-library';
export const LIBRARY_VERSION = 1;

export const exportLibraryQuery = `
  query ExportMCPServerLibrary {
    exportMCPServerLibrary
  }
`;

export const importLibraryMutation = `
  mutation ImportMCPServerLibrary($library: AWSJSON!) {
    importMCPServerLibrary(library: $library) {
      Success
      Message
      Servers {
        Name
        ServerId
        Action
        CredentialsRequired
        Message
      }
    }
  }
`;

export const updateCredentialsMutation = `
  mutation UpdateMCPServerCredentials($input: UpdateMCPServerCredentialsInput!) {
    updateMCPServerCredentials(input: $input) {
      ServerId
      Success
      Message
    }
  }
`;

/** The export's AWSJSON answer as a document, or an error saying what is wrong. */
export const exportedLibrary = (raw) => {
  const document = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (!document || document.kind !== LIBRARY_KIND) {
    throw new Error('The stack did not return an MCP server library.');
  }
  return document;
};

/** The file name an export is saved under. */
export const libraryFileName = (stackName = '', now = new Date()) =>
  `${stackName ? `${stackName}-` : ''}mcp-servers-${now.toISOString().slice(0, 10)}.json`;

/** The text of a library file, checked, as a document to import. */
export const parseLibraryFile = (text) => {
  let document;
  try {
    document = JSON.parse(text);
  } catch (err) {
    throw new Error(`The file is not valid JSON: ${err.message}`);
  }
  if (!document || typeof document !== 'object' || document.kind !== LIBRARY_KIND) {
    throw new Error('The file is not an exported MCP server library.');
  }
  if (document.version !== LIBRARY_VERSION) {
    throw new Error(
      `The library is format version ${document.version}; this release reads version ${LIBRARY_VERSION}.`,
    );
  }
  if (!Array.isArray(document.servers)) {
    throw new Error('The library has no list of servers.');
  }
  return document;
};

/** How many servers of a library will need their credentials entered after an import. */
export const serversNeedingCredentials = (document) =>
  (document?.servers || []).filter((server) => (server.credentialsToReenter || []).length > 0).length;
