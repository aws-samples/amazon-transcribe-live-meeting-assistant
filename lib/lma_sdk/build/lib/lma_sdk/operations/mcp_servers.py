# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""MCP server library operations: export a deployment's servers, import them into another.

Available as ``client.mcp``::

    library = client.mcp.export_library()          # a dict, with no credentials
    Path("mcp-servers.json").write_text(json.dumps(library, indent=2))

    result = other_client.mcp.import_library(library)
    for server in result["Servers"]:
        print(server["Name"], server["Action"], server["CredentialsRequired"])

The export is built by the stack (``exportMCPServerLibrary``) and never holds a
credential: API keys, bearer tokens, OAuth client secrets and access or refresh
tokens are written as empty placeholders, listed in each server's
``credentialsToReenter``. An import creates or updates servers by name and
stores no credential; a server that needs one is marked ``CredentialsRequired``
and is not used by the meeting assistant until an Admin enters it on the MCP
Servers page. Both calls are signed with the caller's AWS credentials (IAM).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, Union

from lma_sdk.exceptions import LMAAppSyncError

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

#: The library document's kind and the format version this SDK writes and reads.
LIBRARY_KIND = "lma-mcp-server-library"
LIBRARY_VERSION = 1

EXPORT_QUERY = "query ExportMCPServerLibrary { exportMCPServerLibrary }"

IMPORT_MUTATION = """
mutation ImportMCPServerLibrary($library: AWSJSON!) {
  importMCPServerLibrary(library: $library) {
    Success
    Message
    Servers { Name ServerId Action CredentialsRequired Message }
  }
}
"""


class McpServerOperations:
    """The MCP server library, as ``client.mcp``."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client

    def export_library(self) -> Dict[str, Any]:
        """The deployment's installed MCP servers as a library document, with no credentials."""
        data = self._client.appsync.graphql(EXPORT_QUERY, operation_name="ExportMCPServerLibrary")
        raw = data.get("exportMCPServerLibrary")
        document = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(document, dict) or document.get("kind") != LIBRARY_KIND:
            raise LMAAppSyncError("exportMCPServerLibrary did not return an MCP server library")
        return document

    def import_library(self, library: Union[Dict[str, Any], str]) -> Dict[str, Any]:
        """Create or update the servers ``library`` lists, matched by name.

        ``library`` is a document from :meth:`export_library` (or its JSON text).
        Whatever it holds in a credential field is ignored by the stack. Returns
        the stack's answer: ``Success``, ``Message`` and, per server, its
        ``Action`` (created, updated, skipped or failed) and whether it now
        waits for ``CredentialsRequired``.

        Raises:
            ValueError: ``library`` is not an MCP server library of this format.
        """
        document = json.loads(library) if isinstance(library, str) else library
        if not isinstance(document, dict) or document.get("kind") != LIBRARY_KIND:
            raise ValueError(f"not an MCP server library (kind {LIBRARY_KIND!r})")
        if document.get("version") != LIBRARY_VERSION:
            raise ValueError(
                f"the library is format version {document.get('version')!r}; "
                f"this SDK reads version {LIBRARY_VERSION}"
            )
        data = self._client.appsync.graphql(
            IMPORT_MUTATION,
            variables={"library": json.dumps(document)},
            operation_name="ImportMCPServerLibrary",
        )
        return data.get("importMCPServerLibrary") or {}
