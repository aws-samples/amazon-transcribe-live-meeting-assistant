# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Behaviour tests for the getChatButtonConfig resolver's response (no AWS).

The chat button record is the one configuration record every signed-in user
reads: the Meeting Assist chat panel renders its quick-action buttons from it for
whoever is viewing a meeting. Its keys carry a sequence number and an
administrator-chosen label, so they cannot be declared as fields of the
`ChatButtonConfig` type, and the resolver returns them as a JSON string instead
-- which means GraphQL applies no field filtering and the resolver is the only
thing deciding what the response contains. That makes the filter worth a test.

The resolver runs in AppSync's APPSYNC_JS runtime, which has no regular
expressions, so the "N#LABEL" check is written out character by character. A test
asserting on the source text would not catch a mistake in that loop, so these
tests extract the resolver from the template and execute it under node with
`@aws-appsync/utils` stubbed. node is a prerequisite of this repository.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"

# The resolver is declared inline in the template rather than as a file under
# source/appsync/, so it is read back out of the template it ships in.
_CODE_BLOCK = re.compile(r"\n      Code: \|\n((?:(?:        .*)?\n)+)")

_IMPORT_LINE = "import { util } from '@aws-appsync/utils';"

# util.error is only reached on the error branch, which these tests do not take.
# Stubbed rather than installed so the test needs no node_modules.
_UTIL_STUB = """
const util = {
  error: (message, type) => { throw new Error(`${type}: ${message}`); },
  dynamodb: { toMapValues: (value) => value },
};
"""


def _resolver_source(logical_id: str) -> str:
    """The inline APPSYNC_JS code of one resolver, dedented, as it is uploaded."""
    template = TEMPLATE.read_text()
    start = template.index(f"\n  {logical_id}:\n")
    match = _CODE_BLOCK.search(template, start)
    assert match, f"{logical_id} has no inline Code block in {TEMPLATE.name}"
    # The block must belong to this resolver, not a later one.
    assert match.start() - start < 2000, f"matched a Code block far from {logical_id}"
    return "".join(
        line[8:] + "\n" if len(line) > 8 else "\n" for line in match.group(1).split("\n")
    )


@pytest.fixture(scope="module")
def resolver_source() -> str:
    source = _resolver_source("GetChatButtonConfigResolver")
    assert _IMPORT_LINE in source, "the resolver no longer imports util as expected"
    return source.replace(_IMPORT_LINE, _UTIL_STUB)


def respond(resolver_source: str, result: object) -> dict:
    """Run the real resolver's `response` over `result` and return what it emits."""
    node = shutil.which("node")
    assert node, (
        "node is required to execute the resolver under test; it is a prerequisite "
        "of this repository (see CLAUDE.md)"
    )
    harness = (
        f"{resolver_source}\n"
        f"const out = response({{ result: {json.dumps(result)} }});\n"
        "console.log(JSON.stringify(out));\n"
    )
    completed = subprocess.run(
        [node, "--input-type=module", "-"],
        input=harness,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"node rejected the resolver:\n{completed.stderr}"
    return json.loads(completed.stdout.strip())


def buttons(resolver_source: str, result: object) -> dict:
    """The decoded button map the UI would parse out of the response."""
    return json.loads(respond(resolver_source, result)["ChatButtonConfigId"])


# ── what the response carries ───────────────────────────────────────────────


def test_the_response_carries_the_button_fields(resolver_source: str) -> None:
    assert buttons(
        resolver_source,
        {"ChatButtonConfigId": "CustomChatButtonConfig", "1#Action Items": "List the actions"},
    ) == {"1#Action Items": "List the actions"}


def test_the_response_carries_only_the_button_fields(resolver_source: str) -> None:
    """Every signed-in user reads this record, so the response is the buttons.

    The stored item also holds the partition key and the attributes recording who
    saved it last, and more may be added later; none of them is part of what a
    reader of the chat buttons asked for.
    """
    assert buttons(
        resolver_source,
        {
            "ChatButtonConfigId": "CustomChatButtonConfig",
            "1#Action Items": "List the actions",
            "2#Summary": "Summarize",
            "UpdatedBy": "admin@example.com",
            "UpdatedAt": "2026-10-22T18:30:00.000000Z",
            "*Information*": "edit these in the UI",
            "Owner": "someone",
        },
    ) == {"1#Action Items": "List the actions", "2#Summary": "Summarize"}


def test_no_address_from_the_audit_attributes_reaches_a_reader(resolver_source: str) -> None:
    """Pinned on the serialized response, not just the decoded map."""
    response = respond(
        resolver_source,
        {
            "ChatButtonConfigId": "CustomChatButtonConfig",
            "1#Summary": "Summarize",
            "UpdatedBy": "admin@example.com",
            "UpdatedAt": "2026-10-22T18:30:00.000000Z",
        },
    )
    assert "admin@example.com" not in json.dumps(response)
    assert "UpdatedBy" not in json.dumps(response)
    assert "UpdatedAt" not in json.dumps(response)


def test_an_absent_record_reads_as_no_buttons(resolver_source: str) -> None:
    """The seed stack creates both records, but a reader should not have to assume it."""
    assert buttons(resolver_source, None) == {}


def test_a_record_holding_no_buttons_reads_as_no_buttons(resolver_source: str) -> None:
    """How a reset to defaults is stored: the key and the audit attributes alone."""
    assert (
        buttons(
            resolver_source,
            {
                "ChatButtonConfigId": "CustomChatButtonConfig",
                "UpdatedBy": "admin@example.com",
                "UpdatedAt": "2026-10-22T18:30:00.000000Z",
            },
        )
        == {}
    )


# ── the sequence-number form, which stands in for /^\d+#/ ───────────────────


@pytest.mark.parametrize(
    "key",
    [
        "1#Summary",
        "12#Summary",
        "0#Summary",
        "3#",  # an empty label is still a button key
        "12#A#B",  # only the first '#' separates the sequence number
        "1#Summary With Spaces",
    ],
)
def test_a_key_in_the_sequence_form_is_a_button(resolver_source: str, key: str) -> None:
    assert list(buttons(resolver_source, {key: "prompt"})) == [key]


@pytest.mark.parametrize(
    "key",
    [
        "ChatButtonConfigId",
        "UpdatedBy",
        "UpdatedAt",
        "*Information*",
        "#Summary",  # no sequence number ahead of the separator
        "1x#Summary",  # not every character ahead of the separator is a digit
        "x1#Summary",
        " 1#Summary",
        "1.5#Summary",
        "Summary",
        "",
    ],
)
def test_a_key_outside_the_sequence_form_is_not_a_button(resolver_source: str, key: str) -> None:
    assert buttons(resolver_source, {key: "prompt"}) == {}
