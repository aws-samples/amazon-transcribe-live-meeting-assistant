#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Every AWSJSON value this Lambda returns reaches the client encoded once.

The same check as ``skills_manager/test_awsjson_encoding.py``, for the one field
of the meeting whiteboards function whose result is AWSJSON:
``meetingPlaybookOptions``. The response template ``$util.toJson($ctx.result)``
writes the result as JSON, AppSync parses it back and serializes the AWSJSON
value as JSON again, so the handler returns an object, never JSON text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import test_playbooks as playbooks_test
from test_index import table  # noqa: F401 (fixture)
from test_playbooks import world  # noqa: F401 (fixture)

AI_STACK = Path(__file__).resolve().parents[3]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-lambda-resolvers.yaml"

TO_JSON_TEMPLATE = re.compile(
    r"\s*#if\( \$ctx\.error \)\s*\$util\.error\(\$ctx\.error\.message, \$ctx\.error\.type\)\s*"
    r"#end\s*\$util\.toJson\(\$ctx\.result\)\s*"
)

#: The fields of this Lambda whose result is AWSJSON.
CASES = [("Query", "meetingPlaybookOptions")]


def _response_template(field_name: str) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    block = next(b for b in re.split(r"\n  (?=\w+:\n)", text) if f"FieldName: {field_name}\n" in b)
    return re.search(r"ResponseMappingTemplate: \|\n((?:        .*\n|\s*\n)+)", block + "\n").group(
        1
    )


def test_the_response_template_writes_the_result_as_json() -> None:
    for _type_name, field_name in CASES:
        assert TO_JSON_TEMPLATE.fullmatch(_response_template(field_name))


def test_the_options_reach_the_client_as_an_object(world) -> None:  # noqa: F811
    result = playbooks_test.options(Title="Sprint 42 planning")
    value = json.loads(json.dumps(result))  # $util.toJson, then AppSync parses it
    client = json.loads(json.dumps(value))  # the AWSJSON value, parsed once
    assert isinstance(client, dict) and isinstance(client["Playbooks"], list)
    assert client["Suggestions"][0]["SkillId"] == "sprint-planning"
