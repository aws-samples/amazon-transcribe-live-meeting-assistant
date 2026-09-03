# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""AppSync Lambda resolver for the updateAsrConfig mutation.

The on-demand ASR engine has exactly three runtime switches, all booleans, all
read at the start of each meeting so a change needs no stack update:

* streamingEngineMicrovm - streaming meetings (Stream Audio, the Desktop Capture
  apps) use the on-demand engine instead of Amazon Transcribe.
* virtualParticipantEngineMicrovm - Virtual Participants use the on-demand engine.
* diarizeVirtualParticipant - a Virtual Participant on the on-demand engine asks
  for per-voice labels, so several people behind one attendee tile come out as
  "Name (spk_0)", "Name (spk_1)" instead of one name.

There are deliberately no tuning fields. The diarization operating point (the
similarity threshold and the minimum utterance length) is measured for the model
bundle and baked into the ASR image; it is not something a deployment should have
to know a number for, and a guessed value fragments or merges speakers.
"""

import json
import os
from typing import Any

import boto3

dynamodb = boto3.resource("dynamodb")

CONFIG_ID = "CustomAsrConfig"

BOOLEAN_FIELDS = frozenset(
    {"streamingEngineMicrovm", "virtualParticipantEngineMicrovm", "diarizeVirtualParticipant"}
)
ALLOWED_FIELDS = BOOLEAN_FIELDS


def lambda_handler(event: dict, context: Any) -> dict:
    """Validate and store the ASR runtime switches.

    Returns ``{"AsrConfigId", "Success"}``. Raises only when the request itself is
    unusable; an unknown field is dropped with a log line rather than failing the
    whole save, matching the other config resolvers.
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
            item[key] = bool(value)

        table.put_item(Item=item)
        print(f"Updated ASR config: {json.dumps({k: str(v) for k, v in item.items()})}")
        return {"AsrConfigId": config_id, "Success": True}

    except Exception as exc:
        print(f"Error updating ASR config: {exc}")
        raise Exception(f"Failed to update ASR config: {exc}") from exc
