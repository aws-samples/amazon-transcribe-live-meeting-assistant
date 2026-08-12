# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""AppSync Lambda resolver for the updateAsrConfig mutation.

Runtime overrides for the MicroVM ASR engine's diarization operating point. These
exist because the operating point is empirical and model-specific: the shipped
threshold was measured on real meeting audio, and re-measuring it for a different
embedder, language or microphone should not require a stack update.

Only fields in ALLOWED_FIELDS are stored, each range-checked. An omitted or blank
field means "use the stack parameter", so this table holds overrides, not a
complete configuration — there is no default record to keep in sync.
"""

import json
import os
from typing import Any

import boto3

dynamodb = boto3.resource("dynamodb")

CONFIG_ID = "CustomAsrConfig"

# Numeric overrides: field -> (minimum, maximum, kind).
NUMERIC_FIELDS = {
    # Cosine similarity. Specific to the speaker model; 0.2 is the measured
    # operating point for TitaNet, where different speakers scored at most 0.107.
    "speakerThreshold": (0.0, 1.0, float),
    # Shortest utterance worth embedding. Below this a segment inherits the current
    # speaker instead of minting an identity from an unreliable embedding.
    "minSegmentMs": (0, 5000, int),
    # 0 discovers as many speakers as appear; a cap is a safety net, not a fix.
    "maxSpeakers": (0, 30, int),
    # Trailing silence that closes an utterance.
    "endpointingMs": (200, 5000, int),
}

BOOLEAN_FIELDS = {
    # Withhold the first unmatched embedding until a second one agrees with it.
    # Off by default: measured to cost attribution purity when the threshold is
    # already correct, and to merge speakers when it is too high.
    "requireCorroboration",
    # Route every streaming meeting to the MicroVM engine, not just those that ask
    # for diarization.
    "engineDefaultMicrovm",
    "diarizeByDefault",
}

ALLOWED_FIELDS = set(NUMERIC_FIELDS) | BOOLEAN_FIELDS


def _coerce_numeric(key: str, value: Any) -> float | int | None:
    minimum, maximum, kind = NUMERIC_FIELDS[key]
    try:
        number = kind(value)
    except (TypeError, ValueError):
        print(f"Invalid {key} (not a {kind.__name__}): {value!r}, skipping")
        return None
    if not minimum <= number <= maximum:
        print(f"Invalid {key} ({number} outside [{minimum}, {maximum}]), skipping")
        return None
    return number


def lambda_handler(event: dict, context: Any) -> dict:
    """Validate and store the ASR runtime overrides.

    Returns ``{"AsrConfigId", "Success"}``. Raises only when the request itself is
    unusable; an individual bad field is dropped with a log line rather than
    failing the whole save, matching the other config resolvers.
    """
    try:
        table = dynamodb.Table(os.environ["ASR_CONFIG_TABLE_NAME"])

        input_data = event["arguments"]["input"]
        config_id = input_data["AsrConfigId"]
        if config_id != CONFIG_ID:
            raise ValueError(f"Only {CONFIG_ID} can be updated")

        try:
            config_object = json.loads(input_data["ConfigData"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in ConfigData: {exc}") from exc
        if not isinstance(config_object, dict):
            raise ValueError("ConfigData must be a JSON object")

        item: dict[str, Any] = {"AsrConfigId": config_id}
        for key, value in config_object.items():
            if key not in ALLOWED_FIELDS:
                print(f"Filtered out non-allowed field: {key}")
                continue
            # An empty string means "unset this override and fall back to the
            # stack parameter", so it is stored as-is rather than coerced to 0.
            if value == "" or value is None:
                item[key] = ""
                continue
            if key in NUMERIC_FIELDS:
                number = _coerce_numeric(key, value)
                if number is None:
                    continue
                item[key] = str(number)
            else:
                item[key] = bool(value)

        table.put_item(Item=item)
        print(f"Updated ASR config: {json.dumps({k: str(v) for k, v in item.items()})}")
        return {"AsrConfigId": config_id, "Success": True}

    except Exception as exc:
        print(f"Error updating ASR config: {exc}")
        raise Exception(f"Failed to update ASR config: {exc}") from exc
