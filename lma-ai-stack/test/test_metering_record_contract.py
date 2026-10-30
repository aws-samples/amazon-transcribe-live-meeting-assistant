# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The metering record has one shape, whichever component writes it.

Four writers produce the records the meeting cost estimate reads:
``lma_metering.records`` (the AI stack's Lambdas, via the configuration layer),
the copy vendored into the meeting-assist stack, and the TypeScript writers in
the WebSocket transcriber and the Virtual Participant. A writer that drifted --
a different partition prefix, a renamed attribute, a unit name the price table
does not use -- would produce records the estimate silently ignores or leaves
unpriced, so the agreement is asserted here rather than left to review.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAYER = ROOT / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from lma_metering import records  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "meetingassist_call_metering",
    ROOT / "lma-meetingassist-setup-stack" / "src" / "call_metering.py",
)
vendored = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vendored)

TS_WRITERS = [
    ROOT / "lma-websocket-transcriber-stack" / "source" / "app" / "src" / "metering.ts",
    ROOT / "lma-virtual-participant-stack" / "backend" / "src" / "metering.ts",
]

PRICING = json.loads(
    (
        ROOT
        / "lma-ai-stack"
        / "source"
        / "lambda_functions"
        / "configuration_seeder"
        / "defaults"
        / "CallPricing.json"
    ).read_text(encoding="utf-8")
)

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_the_vendored_python_writer_builds_the_same_item() -> None:
    usage = {"inputTokens": 1200, "outputTokens": 33.5, "requests": 1}
    kwargs = dict(source="s", expiration_days=90, record_id="r", now=NOW)
    assert vendored.build_item("m-1", "MeetingAssistant", "bedrock/x", usage, **kwargs) == (
        records.build_item("m-1", "MeetingAssistant", "bedrock/x", usage, **kwargs)
    )


def test_the_vendored_python_writer_reads_bedrock_usage_the_same_way() -> None:
    usage = {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7, "cacheReadInputTokens": 0}
    assert vendored.bedrock_usage(usage, requests=3) == records.bedrock_usage(usage, requests=3)
    assert vendored.PK_PREFIX == records.PK_PREFIX
    assert vendored.TABLE_ENV_VAR == records.TABLE_ENV_VAR
    assert vendored.EXPIRATION_ENV_VAR == records.EXPIRATION_ENV_VAR


@pytest.mark.parametrize("path", TS_WRITERS, ids=lambda p: p.parts[-5])
def test_each_typescript_writer_uses_the_shared_key_layout(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert f"'{records.PK_PREFIX}'" in source, f"{path} does not use the {records.PK_PREFIX} prefix"
    for attribute in ("PK", "SK", "CallId", "Context", "Service", "Usage", "Source", "RecordedAt"):
        assert re.search(rf"\b{attribute}:\s*\{{", source), f"{path} does not write {attribute}"
    assert "ExpiresAfter" in source
    assert f"'{records.TABLE_ENV_VAR}'" in source
    assert f"'{records.EXPIRATION_ENV_VAR}'" in source


@pytest.mark.parametrize("path", TS_WRITERS, ids=lambda p: p.parts[-5])
def test_every_service_a_typescript_writer_names_has_a_price_or_a_note(path: Path) -> None:
    """A Service string with no entry would show as unpriced on every meeting."""
    source = path.read_text(encoding="utf-8")
    services = set(re.findall(r"SERVICE_[A-Z0-9_]+\s*=\s*'([^']+)'", source))
    assert services, f"{path} declares no SERVICE_ constants, so this test checks nothing"
    priced = set(PRICING["regions"]["default"]) | set(PRICING.get("notes") or {})
    missing = sorted(s for s in services if s not in priced and not s.startswith("bedrock/"))
    assert not missing, f"{path} meters services the price table does not know: {missing}"
