# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A stand-in for the strands-agents SDK, covering every name LMA imports from it.

The meeting assistant and the troubleshooting agent import ``strands`` from a
Lambda layer, which the unit-test environments do not install. Their tests used
to build small stubs of their own, each holding only what that test needed when
it was written; when the code started importing a name a stub lacked
(``strands.models.CacheConfig``), the handler's ``ImportError`` branch took the
direct-Bedrock fallback instead and the tests made real Amazon Bedrock calls.
This module is the one stub, and :data:`IMPORTED_NAMES` is the list a test holds
equal to the code's ``from strands... import`` lines.

The stand-in is inert. ``tool`` is the identity, so a tool body stays directly
callable; ``BedrockModel`` and ``CacheConfig`` record their arguments; ``Agent``
and ``MCPClient`` refuse to be built, because a test that runs the agent or
loads MCP servers supplies its own (``monkeypatch.setattr(stub, "Agent", ...)``).
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

#: Each module the code imports from, with the names it imports.
IMPORTED_NAMES: dict[str, tuple[str, ...]] = {
    "strands": ("Agent", "tool"),
    "strands.models": ("BedrockModel", "CacheConfig"),
    "strands.hooks.events": ("AfterToolCallEvent", "BeforeToolCallEvent"),
    "strands._async": ("run_async",),
    "strands.tools.mcp": ("MCPClient",),
}

#: Every module registered, parents included.
MODULE_NAMES = (
    "strands",
    "strands.models",
    "strands.hooks",
    "strands.hooks.events",
    "strands._async",
    "strands.tools",
    "strands.tools.mcp",
)


def tool(func: Callable | None = None, **_options: Any) -> Any:
    """``@tool`` and ``@tool(context=True)`` alike leave the function as it is."""
    if func is None:
        return lambda inner: inner
    return func


class Agent:
    """Refuses to be built: a test that runs the agent stands in its own."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "the stand-in strands.Agent was constructed; a test that runs the agent "
            "replaces it (monkeypatch.setattr(strands_stub, 'Agent', ...))"
        )


class BedrockModel:
    """Records the keyword arguments it was built with in ``config``."""

    def __init__(self, **kwargs: Any) -> None:
        self.config = kwargs


class CacheConfig:
    """Records the keyword arguments it was built with as attributes."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CacheConfig) and other.__dict__ == self.__dict__

    __hash__ = None  # type: ignore[assignment]


class BeforeToolCallEvent:
    """The hook event type; only ever passed to ``add_callback``."""


class AfterToolCallEvent:
    """The hook event type; only ever passed to ``add_callback``."""


def run_async(function: Callable[[], Any]) -> Any:
    """Run a coroutine function to completion, as the SDK's helper does."""
    return asyncio.run(function())


class MCPClient:
    """Refuses to be built: a test that loads MCP servers stands in its own."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the stand-in strands.tools.mcp.MCPClient was constructed")


def build() -> dict[str, types.ModuleType]:
    """Fresh stand-in modules, keyed by module name, linked parent to child."""
    modules = {name: types.ModuleType(name) for name in MODULE_NAMES}
    values = globals()
    for name, attributes in IMPORTED_NAMES.items():
        for attribute in attributes:
            setattr(modules[name], attribute, values[attribute])
    for name, module in modules.items():
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(modules[parent], child, module)
    return modules


def install(monkeypatch: Any) -> types.ModuleType:
    """Register the stand-in for one test; ``monkeypatch`` restores what was there."""
    modules = build()
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return modules["strands"]


@contextmanager
def installed() -> Iterator[types.ModuleType]:
    """Register the stand-in for a block (a module-scoped fixture), then restore."""
    previous = {name: sys.modules.get(name) for name in MODULE_NAMES}
    modules = build()
    sys.modules.update(modules)
    try:
        yield modules["strands"]
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
