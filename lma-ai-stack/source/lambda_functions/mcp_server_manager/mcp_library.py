#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The MCP server library document: a deployment's installed servers, without credentials.

An export lists every installed server's definition so that another deployment
can install the same servers. It is built from an **allowlist**: each field of
the document is copied from a field named here, and nothing else in a server
record reaches it. In particular the stored ``AuthConfig`` is never copied; the
export describes the server's authentication by its type and the names of what
it needs (header names, environment variable names, OAuth client settings), and
writes every credential as an empty placeholder listed in the server's
``credentialsToReenter``. The credentials it never carries:

- a bearer token;
- the values of custom headers and environment variables (API keys);
- an OAuth client secret;
- OAuth access and refresh tokens.

The document::

    {
      "kind": "lma-mcp-server-library",
      "version": 1,
      "exportedAt": "2026-11-19T12:00:00Z",
      "servers": [
        {
          "serverId": "...", "name": "...", "packageType": "streamable-http",
          "package": "...", "version": "latest", "transport": ["streamable-http"],
          "serverUrl": "https://...", "requiresAuth": true, "description": "...",
          "auth": {"authType": "bearer", "token": ""},
          "credentialsToReenter": ["auth.token"]
        }
      ]
    }

``kind`` and ``version`` are the document's type and format version, so that a
library of another kind (skills, later) can follow the same pattern with a kind
of its own.

An import (``plan_import``) reads such a document and decides, per server and
matched to the installed servers **by name**, whether it creates a server or
updates one. Whatever the document holds in a credential field is ignored: an
imported server's credentials are always entered again on the receiving
deployment, and a server that needs them is marked ``CredentialsRequired`` until
they are, which keeps the meeting assistant from loading it.

This module does no I/O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

LIBRARY_KIND = "lma-mcp-server-library"
LIBRARY_VERSION = 1

PACKAGE_TYPES = ("pypi", "npm", "streamable-http")

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_CUSTOM_HEADERS = "custom_headers"
AUTH_ENV_VARS = "env_vars"
AUTH_OAUTH2 = "oauth2"
AUTH_OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"
#: Every authentication type the servers table holds, and "none".
AUTH_TYPES = (
    AUTH_NONE,
    AUTH_BEARER,
    AUTH_CUSTOM_HEADERS,
    AUTH_ENV_VARS,
    AUTH_OAUTH2,
    AUTH_OAUTH_CLIENT_CREDENTIALS,
)
#: Written for a stored type this release does not know. Its settings are not
#: exported, since nothing says which of them are credentials.
AUTH_UNKNOWN = "unknown"

#: The credential fields of each type, as paths in the exported server. Each is
#: written empty and listed in the server's ``credentialsToReenter``.
CREDENTIAL_PATHS: Dict[str, Sequence[str]] = {
    AUTH_NONE: (),
    AUTH_BEARER: ("auth.token",),
    AUTH_CUSTOM_HEADERS: ("auth.headers",),
    AUTH_ENV_VARS: ("auth.env",),
    AUTH_OAUTH2: ("auth.oauth.accessToken", "auth.oauth.refreshToken"),
    AUTH_OAUTH_CLIENT_CREDENTIALS: ("auth.oauth.clientSecret",),
    AUTH_UNKNOWN: ("auth",),
}

#: The OAuth settings that are not credentials, per type.
OAUTH_SETTINGS: Dict[str, Sequence[str]] = {
    AUTH_OAUTH2: ("provider", "clientId", "authorizationUrl", "tokenUrl", "scopes"),
    AUTH_OAUTH_CLIENT_CREDENTIALS: ("grantType", "clientId", "tokenUrl", "scopes"),
}

#: A header name as HTTP defines one (a token), and an environment variable name.
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
AUTH_TYPE_NAME = re.compile(r"[a-z0-9_]{1,64}")

MAX_SERVERS = 50
MAX_TEXT = 2048


class LibraryError(ValueError):
    """The document is not an MCP server library this release can import."""


def _text(value: Any, limit: int = MAX_TEXT) -> Optional[str]:
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


def _auth_data(auth_config: Any) -> Optional[Mapping[str, Any]]:
    """The stored AuthConfig as an object: it is held as JSON text or as a map."""
    if isinstance(auth_config, str):
        try:
            auth_config = json.loads(auth_config)
        except (TypeError, ValueError):
            return None
    return auth_config if isinstance(auth_config, Mapping) else None


def _stored_settings(server: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The server's AuthConfig; else, for an imported server waiting for its
    credentials, the settings the import recorded (``ImportedAuth``, which holds
    no credential)."""
    return _auth_data(server.get("AuthConfig")) or _auth_data(server.get("ImportedAuth"))


def stored_auth_type(server: Mapping[str, Any]) -> str:
    """The authentication type of a stored server: ``none``, a known type, or ``unknown``."""
    data = _stored_settings(server)
    if not data:
        required = server.get("RequiredAuthType")
        if required == AUTH_UNKNOWN:
            return AUTH_UNKNOWN
        return required if required in AUTH_TYPES else AUTH_NONE
    # The meeting assistant reads an AuthConfig without a type as a bearer token.
    auth_type = data.get("authType", AUTH_BEARER)
    return auth_type if auth_type in AUTH_TYPES else AUTH_UNKNOWN


def _names(values: Any, pattern: re.Pattern) -> List[str]:
    if not isinstance(values, Mapping):
        return []
    return sorted(name for name in values if isinstance(name, str) and pattern.fullmatch(name))


def export_auth(server: Mapping[str, Any]) -> Dict[str, Any]:
    """A server's authentication as the export writes it: settings, with every credential empty."""
    auth_type = stored_auth_type(server)
    data = _stored_settings(server) or {}
    if auth_type == AUTH_NONE:
        return {"authType": AUTH_NONE}
    if auth_type == AUTH_BEARER:
        return {"authType": AUTH_BEARER, "token": ""}
    if auth_type == AUTH_CUSTOM_HEADERS:
        # Header names only. On a package server the loader reads the same
        # settings from `env`, so names from either are kept.
        names = _names(data.get("headers"), HEADER_NAME) or _names(data.get("env"), HEADER_NAME)
        return {"authType": AUTH_CUSTOM_HEADERS, "headers": {name: "" for name in names}}
    if auth_type == AUTH_ENV_VARS:
        return {
            "authType": AUTH_ENV_VARS,
            "env": {name: "" for name in _names(data.get("env"), ENV_NAME)},
        }
    if auth_type in OAUTH_SETTINGS:
        stored = data.get("oauth") if isinstance(data.get("oauth"), Mapping) else {}
        oauth: Dict[str, Any] = {}
        for key in OAUTH_SETTINGS[auth_type]:
            value = stored.get(key)
            if key == "scopes":
                if isinstance(value, list):
                    oauth[key] = [s for s in value if _text(s, 256)]
            elif _text(value):
                oauth[key] = value
        if auth_type == AUTH_OAUTH2:
            oauth.update(accessToken="", refreshToken="")
        else:
            oauth["clientSecret"] = ""
        return {"authType": auth_type, "oauth": oauth}
    return {"authType": AUTH_UNKNOWN}


def export_server(server: Mapping[str, Any]) -> Dict[str, Any]:
    """One installed server as a library entry. Only allowlisted fields are copied."""
    entry: Dict[str, Any] = {}
    for key, source in (
        ("serverId", "ServerId"),
        ("name", "Name"),
        ("packageType", "PackageType"),
        ("package", "NpmPackage"),
        ("version", "Version"),
        ("serverUrl", "ServerUrl"),
        ("description", "Description"),
    ):
        value = _text(server.get(source))
        if value is not None:
            entry[key] = value
    transport = server.get("Transport")
    if isinstance(transport, (list, tuple)):
        entry["transport"] = [t for t in transport if _text(t, 64)]
    entry["requiresAuth"] = server.get("RequiresAuth") is True
    auth = export_auth(server)
    entry["auth"] = auth
    entry["credentialsToReenter"] = list(CREDENTIAL_PATHS[auth["authType"]])
    return entry


def export_library(
    servers: Sequence[Mapping[str, Any]], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The library document for ``servers``, the stored records, sorted by name."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = sorted(
        servers, key=lambda s: (str(s.get("Name") or ""), str(s.get("ServerId") or ""))
    )
    return {
        "kind": LIBRARY_KIND,
        "version": LIBRARY_VERSION,
        "exportedAt": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "servers": [export_server(server) for server in ordered],
    }


# ── import ──────────────────────────────────────────────────────────────────


@dataclass
class ImportedServer:
    """One library entry, checked, with nothing from its credential fields."""

    server_id: str
    name: str
    package_type: str
    package: str
    version: str
    transport: List[str]
    server_url: Optional[str]
    description: Optional[str]
    auth_type: str
    requires_auth: bool
    #: The authentication settings, as export_auth writes them: no credential.
    auth: Dict[str, Any] = field(default_factory=dict)

    @property
    def needs_credentials(self) -> bool:
        """The server cannot be used until its credentials are entered."""
        return self.auth_type != AUTH_NONE


@dataclass
class ImportStep:
    """What the import does with one entry."""

    name: str
    action: str  # "create", "update" or "skip"
    server_id: str = ""
    server: Optional[ImportedServer] = None
    keep_credentials: bool = False
    message: str = ""


@dataclass
class ImportPlan:
    """Every entry's step, in the document's order."""

    steps: List[ImportStep] = field(default_factory=list)


def parse_library(document: Any) -> List[Mapping[str, Any]]:
    """The server entries of a library document, or LibraryError naming what is wrong."""
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError as exc:
            raise LibraryError(f"The file is not valid JSON: {exc}") from None
    if not isinstance(document, Mapping) or document.get("kind") != LIBRARY_KIND:
        raise LibraryError(f"The file is not an MCP server library (kind {LIBRARY_KIND!r}).")
    if document.get("version") != LIBRARY_VERSION:
        raise LibraryError(
            f"The library is format version {document.get('version')!r}; "
            f"this release reads version {LIBRARY_VERSION}."
        )
    servers = document.get("servers")
    if not isinstance(servers, list):
        raise LibraryError("The library has no list of servers.")
    if len(servers) > MAX_SERVERS:
        raise LibraryError(
            f"The library lists {len(servers)} servers; at most {MAX_SERVERS} are read."
        )
    return servers


def _imported(entry: Any, validate_package: Callable[[str, str], str]) -> ImportedServer:
    if not isinstance(entry, Mapping):
        raise LibraryError("an entry is not an object")
    name = _text(entry.get("name"), 256)
    if not name:
        raise LibraryError("an entry has no name")
    server_id = _text(entry.get("serverId"), 256) or name
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in server_id):
        raise LibraryError(f"{name}: the server id holds control characters")
    package_type = entry.get("packageType") or "pypi"
    if package_type not in PACKAGE_TYPES:
        raise LibraryError(f"{name}: package type {package_type!r} is not one of {PACKAGE_TYPES}")
    server_url = _text(entry.get("serverUrl"))
    # A remote server is addressed by its URL, which is also what an install
    # stores as its package.
    package = entry.get("package")
    if package_type == "streamable-http":
        package = server_url or package
    package = validate_package(package, package_type)
    transport = entry.get("transport")
    transport = (
        [t for t in transport if _text(t, 64)][:8]
        if isinstance(transport, list)
        else (["streamable-http"] if package_type == "streamable-http" else ["stdio"])
    )
    auth = entry.get("auth") if isinstance(entry.get("auth"), Mapping) else {}
    auth_type = auth.get("authType", AUTH_NONE)
    if not isinstance(auth_type, str) or not AUTH_TYPE_NAME.fullmatch(auth_type):
        raise LibraryError(f"{name}: the authentication type is not a type name")
    if auth_type not in AUTH_TYPES:
        # Kept as a requirement, so the server waits for its credentials.
        auth_type = AUTH_UNKNOWN
    return ImportedServer(
        server_id=server_id,
        name=name,
        package_type=package_type,
        package=package,
        version=_text(entry.get("version"), 128) or "latest",
        transport=transport,
        server_url=server_url,
        description=_text(entry.get("description")),
        auth_type=auth_type,
        requires_auth=entry.get("requiresAuth") is True or auth_type != AUTH_NONE,
        # Re-read through the export's allowlist, so whatever the document holds
        # in a credential field is dropped here.
        auth=(
            export_auth({"AuthConfig": {**auth, "authType": auth_type}})
            if auth_type != AUTH_UNKNOWN
            else {"authType": AUTH_UNKNOWN}
        ),
    )


def plan_import(
    document: Any,
    installed: Sequence[Mapping[str, Any]],
    validate_package: Callable[[str, str], str],
    max_servers: int,
) -> ImportPlan:
    """What importing ``document`` over the ``installed`` server records does.

    Matched by name: an entry whose name an installed server has updates that
    server, keeping its server id, and keeps its stored credentials when the
    entry's authentication type is the one the server already has (or none);
    otherwise the credentials are dropped and must be entered again. An entry no
    installed server is named for creates a server, under the entry's server id
    unless another server already uses it, up to ``max_servers`` servers in all.
    An entry that cannot be read is skipped, with the reason, and the others are
    still imported.

    Raises:
        LibraryError: the document itself is not a library this release reads.
    """
    entries = parse_library(document)
    by_name = {str(s.get("Name")): s for s in installed if s.get("Name")}
    used_ids = {str(s.get("ServerId")) for s in installed if s.get("ServerId")}
    count = len(installed)
    seen = set()
    plan = ImportPlan()
    for entry in entries:
        label = (entry.get("name") if isinstance(entry, Mapping) else None) or "(unnamed)"
        try:
            server = _imported(entry, validate_package)
        except (LibraryError, ValueError) as exc:
            plan.steps.append(ImportStep(name=str(label), action="skip", message=str(exc)))
            continue
        if server.name in seen:
            plan.steps.append(
                ImportStep(name=server.name, action="skip", message="listed more than once")
            )
            continue
        seen.add(server.name)
        existing = by_name.get(server.name)
        if existing is not None:
            current = stored_auth_type(existing)
            keep = server.auth_type == AUTH_NONE or (
                current == server.auth_type and not existing.get("CredentialsRequired")
            )
            plan.steps.append(
                ImportStep(
                    name=server.name,
                    action="update",
                    server_id=str(existing["ServerId"]),
                    server=server,
                    keep_credentials=keep,
                )
            )
            continue
        if count >= max_servers:
            plan.steps.append(
                ImportStep(
                    name=server.name,
                    action="skip",
                    message=f"at most {max_servers} servers can be installed",
                )
            )
            continue
        if server.server_id in used_ids:
            plan.steps.append(
                ImportStep(
                    name=server.name,
                    action="skip",
                    message=f"another installed server already has the id {server.server_id}",
                )
            )
            continue
        used_ids.add(server.server_id)
        count += 1
        plan.steps.append(
            ImportStep(name=server.name, action="create", server_id=server.server_id, server=server)
        )
    return plan
