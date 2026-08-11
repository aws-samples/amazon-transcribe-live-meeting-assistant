"""Smoke test: ensures pytest collects and runs with a green suite."""

from __future__ import annotations


def test_scaffold_imports() -> None:
    """The packages that ship in the image import cleanly.

    The upstream prototype's ``router`` package is deliberately absent: its
    control-plane role (launch, token, terminate) is filled by the stack's
    asr_launcher Lambda, and ``asr_microvm`` provides the lifecycle hooks.
    """
    import asr_microvm  # noqa: F401
    import asr_protocol  # noqa: F401
    import asr_server  # noqa: F401

    assert True
