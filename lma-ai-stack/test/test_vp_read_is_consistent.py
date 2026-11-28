# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""getVirtualParticipant reads the Virtual Participant's row consistently.

The call event processor reads a Virtual Participant's row through this
resolver right after creating its meeting, to publish the requests to be added
kept on the row (call_event_processor/participant_list_requests.py). The
request side reads the row and the meeting consistently after keeping a request
(advance_consent_resolver, tested in its test_index.py). With both sides
consistent, a request kept while the meeting is being created is read by at
least one of them; see docs/meeting-consent.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ai_stack_templates import flattened


def _resolver(field: str) -> dict:
    resolvers = [
        resource["Properties"]
        for resource in flattened()["Resources"].values()
        if resource.get("Type") == "AWS::AppSync::Resolver"
        and resource["Properties"].get("TypeName") == "Query"
        and resource["Properties"].get("FieldName") == field
    ]
    assert len(resolvers) == 1, f"expected one Query.{field} resolver, found {len(resolvers)}"
    return resolvers[0]


def _request(properties: dict) -> dict:
    template = properties["RequestMappingTemplate"]
    # The one VTL expression in it, replaced so the rest parses as JSON.
    return json.loads(re.sub(r"\$util\.dynamodb\.toDynamoDBJson\([^)]*\)", '"id"', template))


def test_get_virtual_participant_is_a_consistent_get_item() -> None:
    request = _request(_resolver("getVirtualParticipant"))
    assert request["operation"] == "GetItem"
    assert request["consistentRead"] is True


def test_the_call_event_processor_reads_the_row_through_get_virtual_participant() -> None:
    """The consistency above is what the START's read relies on, so it must be this read."""
    module = (
        Path(__file__).resolve().parents[1]
        / "source"
        / "lambda_functions"
        / "call_event_processor"
        / "participant_list_requests.py"
    ).read_text(encoding="utf-8")
    assert "dsl.Query.getVirtualParticipant" in module
