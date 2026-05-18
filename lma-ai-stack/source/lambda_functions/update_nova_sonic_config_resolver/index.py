"""
Lambda function resolver for updateNovaSonicConfig mutation
Implements input validation and security filtering

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
from typing import Any, Dict

import boto3

dynamodb = boto3.resource("dynamodb")

# Allowlist of valid Nova Sonic configuration fields
ALLOWED_FIELDS = {
    "systemPrompt",
    "promptMode",
    "modelId",
    "voiceId",
    "endpointingSensitivity",
    "groupMeetingMode",  # legacy; preserved for back-compat
    "meetingMode",  # new canonical meeting-mode selector
    "translatorLanguageA",  # only relevant when meetingMode='translator'
    "translatorLanguageB",  # only relevant when meetingMode='translator'
    "translatorMutePhrases",  # comma-separated; translator-mode pause triggers
    "translatorUnmutePhrases",  # comma-separated; translator-mode resume triggers
}

# Valid values for enum-like fields
VALID_PROMPT_MODES = {"base", "inject", "replace"}
VALID_SENSITIVITY_LEVELS = {"LOW", "MEDIUM", "HIGH"}
VALID_MEETING_MODES = {"normal", "group", "translator"}

# Maximum length for translator language labels (prevents huge strings in config).
MAX_LANGUAGE_LABEL_LEN = 64

# Maximum length for the comma-separated translator trigger-phrase strings.
MAX_TRIGGER_PHRASES_LEN = 256


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AppSync Lambda resolver for updateNovaSonicConfig

    Validates and filters input to only allow known Nova Sonic configuration fields.

    Args:
        event: AppSync event with arguments and identity
        context: Lambda context

    Returns:
        Dict with NovaSonicConfigId and Success status
    """
    try:
        # Get table name from environment
        table_name = os.environ["NOVA_SONIC_CONFIG_TABLE_NAME"]
        table = dynamodb.Table(table_name)

        # Extract input from AppSync event
        input_data = event["arguments"]["input"]
        config_id = input_data["NovaSonicConfigId"]
        config_str = input_data["ConfigData"]

        # Only allow updating the Custom config
        if config_id != "CustomNovaSonicConfig":
            raise ValueError("Only CustomNovaSonicConfig can be updated")

        # Parse the JSON configuration
        try:
            config_object = json.loads(config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in ConfigData: {str(e)}")

        # Build item with only allowed fields
        item = {"NovaSonicConfigId": config_id}

        for key, value in config_object.items():
            if key not in ALLOWED_FIELDS:
                print(f"Filtered out non-allowed field: {key}")
                continue

            # Validate enum-like fields
            if key == "promptMode" and value not in VALID_PROMPT_MODES:
                print(f"Invalid promptMode value: {value}, skipping")
                continue
            if key == "endpointingSensitivity" and value not in VALID_SENSITIVITY_LEVELS:
                print(f"Invalid endpointingSensitivity value: {value}, skipping")
                continue
            if key == "meetingMode" and value not in VALID_MEETING_MODES:
                print(f"Invalid meetingMode value: {value}, skipping")
                continue
            if key == "groupMeetingMode":
                # Ensure boolean
                value = bool(value)
            if key in ("translatorLanguageA", "translatorLanguageB"):
                if not isinstance(value, str):
                    print(f"Invalid {key} (not a string): {value!r}, skipping")
                    continue
                value = value.strip()
                if not value:
                    print(f"Invalid {key} (empty string), skipping")
                    continue
                if len(value) > MAX_LANGUAGE_LABEL_LEN:
                    print(
                        f"Invalid {key} (length {len(value)} > {MAX_LANGUAGE_LABEL_LEN}), truncating"
                    )
                    value = value[:MAX_LANGUAGE_LABEL_LEN]
            if key in ("translatorMutePhrases", "translatorUnmutePhrases"):
                if not isinstance(value, str):
                    print(f"Invalid {key} (not a string): {value!r}, skipping")
                    continue
                value = value.strip()
                if not value:
                    print(f"Invalid {key} (empty string), skipping")
                    continue
                if len(value) > MAX_TRIGGER_PHRASES_LEN:
                    print(
                        f"Invalid {key} (length {len(value)} > {MAX_TRIGGER_PHRASES_LEN}), truncating"
                    )
                    value = value[:MAX_TRIGGER_PHRASES_LEN]
            item[key] = value

        # Store in DynamoDB
        table.put_item(Item=item)

        print(f"Successfully updated Nova Sonic config: {config_id}")

        return {"NovaSonicConfigId": config_id, "Success": True}

    except Exception as e:
        print(f"Error updating Nova Sonic config: {str(e)}")
        raise Exception(f"Failed to update Nova Sonic config: {str(e)}")
