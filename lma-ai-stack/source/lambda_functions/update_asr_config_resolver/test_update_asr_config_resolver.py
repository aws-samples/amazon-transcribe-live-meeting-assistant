# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the updateAsrConfig resolver (no AWS calls)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("ASR_CONFIG_TABLE_NAME", "asr-config")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, str(Path(__file__).parent))

with mock.patch("boto3.resource"):
    import index  # noqa: E402


def invoke(config: object, config_id: str = "CustomAsrConfig") -> tuple[dict, dict]:
    """Run the resolver against a fake table; returns (response, stored item)."""
    stored: dict = {}
    table = mock.Mock()
    table.put_item.side_effect = lambda Item: stored.update(Item)  # noqa: N803
    with mock.patch.object(index.dynamodb, "Table", return_value=table):
        response = index.lambda_handler(
            {"arguments": {"input": {"AsrConfigId": config_id, "ConfigData": json.dumps(config)}}},
            None,
        )
    return response, stored


def test_the_switches_are_stored_as_booleans() -> None:
    response, stored = invoke(
        {
            "streamingEngineMicrovm": True,
            "virtualParticipantEngineMicrovm": False,
            "diarizeVirtualParticipant": False,
        }
    )

    assert response == {"AsrConfigId": "CustomAsrConfig", "Success": True}
    assert stored == {
        "AsrConfigId": "CustomAsrConfig",
        "streamingEngineMicrovm": True,
        "virtualParticipantEngineMicrovm": False,
        "diarizeVirtualParticipant": False,
    }


def test_truthy_strings_from_an_older_client_become_booleans() -> None:
    _, stored = invoke({"streamingEngineMicrovm": "true"})

    assert stored["streamingEngineMicrovm"] is True


def test_retired_tuning_fields_are_filtered_out() -> None:
    """The threshold and its friends live in the image now; a stale client that
    still sends them must not be able to put a number back into the table."""
    _, stored = invoke(
        {
            "streamingEngineMicrovm": True,
            "engineDefaultMicrovm": True,
            "speakerThreshold": 0.3,
            "minSegmentMs": 2500,
            "maxSpeakers": 4,
            "liveTurnCut": False,
            "modelId": "../../etc/passwd",
        }
    )

    assert set(stored) == {"AsrConfigId", "streamingEngineMicrovm"}


def test_only_the_custom_record_can_be_written() -> None:
    with pytest.raises(Exception, match="Only CustomAsrConfig"):
        invoke({"streamingEngineMicrovm": True}, config_id="DefaultAsrConfig")


def test_malformed_json_is_rejected() -> None:
    table = mock.Mock()
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=table),
        pytest.raises(Exception, match="Invalid JSON"),
    ):
        index.lambda_handler(
            {"arguments": {"input": {"AsrConfigId": "CustomAsrConfig", "ConfigData": "{not json"}}},
            None,
        )
    table.put_item.assert_not_called()


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        invoke([1, 2, 3])


def test_the_allow_list_is_exactly_the_three_switches() -> None:
    """Adding a field here means adding it to the AppSync type, the transcriber's
    and the VP's readers and the ASR Config page - fail loudly if it drifts."""
    assert index.ALLOWED_FIELDS == {
        "streamingEngineMicrovm",
        "virtualParticipantEngineMicrovm",
        "diarizeVirtualParticipant",
    }
