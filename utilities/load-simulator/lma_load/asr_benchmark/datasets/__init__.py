# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Ground-truth recordings for the ASR benchmark, built from public corpora."""

from __future__ import annotations

from pathlib import Path

DEFAULT_CACHE = Path.home() / ".cache" / "lma-asr-benchmark"


def available() -> list[str]:
    from lma_load.asr_benchmark.datasets import ami

    return [f"ami/{meeting}" for meeting in ami.manifest()["meetings"]]


def prepare(name: str, cache_dir: Path = DEFAULT_CACHE) -> tuple[Path, Path]:
    from lma_load.asr_benchmark.datasets import ami

    corpus, _, meeting = name.partition("/")
    if corpus != "ami" or meeting not in ami.manifest()["meetings"]:
        raise ValueError(f"unknown dataset {name!r}; available: {', '.join(available())}")
    return ami.build_wav(meeting, cache_dir), ami.reference_path(meeting)
