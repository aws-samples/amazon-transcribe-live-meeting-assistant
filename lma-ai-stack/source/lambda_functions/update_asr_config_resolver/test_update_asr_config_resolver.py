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


def invoke(config: dict, config_id: str = "CustomAsrConfig") -> tuple[dict, dict]:
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


def test_valid_overrides_are_stored() -> None:
    response, stored = invoke(
        {"speakerThreshold": 0.2, "minSegmentMs": 2500, "maxSpeakers": 0, "endpointingMs": 1200}
    )

    assert response == {"AsrConfigId": "CustomAsrConfig", "Success": True}
    assert stored["speakerThreshold"] == "0.2"
    assert stored["minSegmentMs"] == "2500"
    assert stored["endpointingMs"] == "1200"


def test_booleans_are_stored_as_booleans() -> None:
    _, stored = invoke({"requireCorroboration": True, "engineDefaultMicrovm": False})

    assert stored["requireCorroboration"] is True
    assert stored["engineDefaultMicrovm"] is False


def test_an_empty_value_clears_the_override() -> None:
    """Blank means "fall back to the stack parameter", not zero."""
    _, stored = invoke({"speakerThreshold": "", "maxSpeakers": None})

    assert stored["speakerThreshold"] == ""
    assert stored["maxSpeakers"] == ""


def test_unknown_fields_are_filtered_out() -> None:
    _, stored = invoke({"speakerThreshold": 0.3, "modelId": "../../etc/passwd", "evil": 1})

    assert "modelId" not in stored
    assert "evil" not in stored
    assert stored["speakerThreshold"] == "0.3"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("speakerThreshold", 1.5),
        ("speakerThreshold", -0.1),
        ("speakerThreshold", "abc"),
        ("minSegmentMs", 99999),
        ("maxSpeakers", 31),
        ("endpointingMs", 10),
    ],
)
def test_out_of_range_values_are_dropped_not_stored(field: str, value: object) -> None:
    """A bad field is dropped with a log line; the rest of the save still lands."""
    _, stored = invoke({field: value, "requireCorroboration": True})

    assert field not in stored
    assert stored["requireCorroboration"] is True


def test_only_the_custom_record_can_be_written() -> None:
    with pytest.raises(Exception, match="Only CustomAsrConfig"):
        invoke({"speakerThreshold": 0.2}, config_id="DefaultAsrConfig")


def test_malformed_json_is_rejected() -> None:
    table = mock.Mock()
    with mock.patch.object(index.dynamodb, "Table", return_value=table), pytest.raises(
        Exception, match="Invalid JSON"
    ):
        index.lambda_handler(
            {"arguments": {"input": {"AsrConfigId": "CustomAsrConfig", "ConfigData": "{not json"}}},
            None,
        )
    table.put_item.assert_not_called()


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        invoke([1, 2, 3])  # type: ignore[arg-type]


def test_the_threshold_range_covers_the_measured_operating_points() -> None:
    """Catalog thresholds span 0.2 (TitaNet) to 0.8 (WeSpeaker ResNet293)."""
    low, high, _ = index.NUMERIC_FIELDS["speakerThreshold"]

    assert low <= 0.2 and high >= 0.8


def test_the_speaker_change_split_is_stored_as_a_boolean() -> None:
    """The knob that turns intra-utterance splitting off without an image rebuild."""
    response, stored = invoke({"splitOnSpeakerChange": False, "requireCorroboration": True})

    assert response["Success"] is True
    assert stored["splitOnSpeakerChange"] is False
    assert stored["requireCorroboration"] is True


def test_the_live_speaker_change_cut_is_stored_as_a_boolean() -> None:
    """Distinct from splitOnSpeakerChange: that splits retroactively, this one live."""
    _, stored = invoke({"liveTurnCut": False, "splitOnSpeakerChange": True})

    assert stored["liveTurnCut"] is False
    assert stored["splitOnSpeakerChange"] is True


def test_the_live_cut_timings_are_stored_as_strings() -> None:
    _, stored = invoke({"turnCutIntervalMs": 1500, "maxOpenSegmentMs": 20000})

    assert stored["turnCutIntervalMs"] == "1500"
    assert stored["maxOpenSegmentMs"] == "20000"


def test_disabling_the_open_row_bound_is_allowed() -> None:
    """0 means "follow the engine's own utterance boundaries", so it must survive.

    The minimum for the other durations is non-zero, so treating them alike would
    silently drop the one value that turns this bound off.
    """
    _, stored = invoke({"maxOpenSegmentMs": 0})

    assert stored["maxOpenSegmentMs"] == "0"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turnCutIntervalMs", 100),  # too frequent: a segmentation window per 100ms
        ("turnCutIntervalMs", 20000),
        ("maxOpenSegmentMs", -1),
        ("maxOpenSegmentMs", 120000),
    ],
)
def test_live_cut_timings_out_of_range_are_dropped(field: str, value: object) -> None:
    _, stored = invoke({field: value, "liveTurnCut": True})

    assert field not in stored
    assert stored["liveTurnCut"] is True


def test_the_open_row_bound_covers_the_transcribe_path_default() -> None:
    """Both engines should be comparable, so 20s must be a legal value here too."""
    low, high, _ = index.NUMERIC_FIELDS["maxOpenSegmentMs"]

    assert low == 0
    assert high >= 20000
