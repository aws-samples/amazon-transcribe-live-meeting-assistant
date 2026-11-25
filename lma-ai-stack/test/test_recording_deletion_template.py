# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""What the AI stack grants and tells the deletion function for the recordings (no AWS).

"Delete the recordings after the summary" is carried out by the transcript
deletion function (docs/transcript-deletion.md). Pinned here: it can list and
delete every version under each recording prefix it is told, and it is told
the prefixes the producers write under.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

import pytest
from ai_stack_templates import flattened

#: Each prefix the function deletes recordings under, as its environment names
#: it, and as the template's policies name it.
PREFIXES = {
    "AUDIO_PREFIX": '{"Ref": "AudioFilePrefix"}',
    "VIDEO_PREFIX": "lma-video-recordings/",
    "VIDEO_CHUNKS_PREFIX": "lma-video-chunks/",
}
IN_POLICY = {
    "AUDIO_PREFIX": "${AudioFilePrefix}*",
    "VIDEO_PREFIX": "lma-video-recordings/*",
    "VIDEO_CHUNKS_PREFIX": "lma-video-chunks/*",
}


@pytest.fixture(scope="module")
def ai() -> Dict[str, Any]:
    return flattened()["Resources"]


def _statements(role: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for policy in role["Properties"].get("Policies", []):
        if isinstance(policy, dict) and "PolicyDocument" in policy:
            yield from policy["PolicyDocument"]["Statement"]


def _actions(statement: Dict[str, Any]) -> List[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


def test_the_function_is_told_each_recording_prefix(ai):
    variables = ai["TranscriptDeletionFunction"]["Properties"]["Environment"]["Variables"]
    for name, value in PREFIXES.items():
        assert json.dumps(variables[name]).strip('"') == value, name


@pytest.mark.parametrize("name", list(IN_POLICY))
def test_the_function_can_list_and_delete_every_version_under_each_prefix(ai, name):
    statements = list(_statements(ai["TranscriptDeletionFunctionExecutionRole"]))
    deletes = json.dumps(
        [s["Resource"] for s in statements if "s3:DeleteObjectVersion" in _actions(s)]
    )
    listing = json.dumps(
        [s.get("Condition") for s in statements if "s3:ListBucketVersions" in _actions(s)]
    )
    assert IN_POLICY[name] in deletes, name
    assert IN_POLICY[name] in listing, name


def test_the_processor_can_tell_the_function_a_recording_was_stored(ai):
    variables = ai["CallEventProcessorFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["TRANSCRIPT_DELETION_FUNCTION_ARN"] == {
        "Fn::GetAtt": "TranscriptDeletionFunction.Arn"
    }
