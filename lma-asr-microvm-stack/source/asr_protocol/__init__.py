"""Shared wire-protocol models for the streaming ASR system.

This package holds the pydantic (v2) models for every WebSocket message defined
in ``design.md`` §5 (config, ready, partial, final, termination, error,
warning, eos). Both the ASR server (inside the MicroVM) and the control-plane
router import these models so the wire contract cannot drift between them.
"""

from __future__ import annotations

from asr_protocol.messages import (
    ClientMessage,
    Config,
    Eos,
    Error,
    Final,
    Partial,
    Ready,
    ServerMessage,
    Termination,
    Warning,
    Word,
    parse_client_message,
    parse_server_message,
)

__all__ = [
    "Config",
    "Eos",
    "Ready",
    "Partial",
    "Word",
    "Final",
    "Termination",
    "Error",
    "Warning",
    "ClientMessage",
    "ServerMessage",
    "parse_client_message",
    "parse_server_message",
]
