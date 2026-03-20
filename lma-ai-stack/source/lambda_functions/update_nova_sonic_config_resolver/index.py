"""
Lambda function resolver for updateNovaSonicConfig mutation
Implements input validation and security filtering

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""
import json
import boto3
import os
from typing import Dict, Any

dynamodb = boto3.resource('dynamodb')

# Allowlist of valid Nova Sonic configuration fields
ALLOWED_FIELDS = {
    'systemPrompt',
    'promptMode',
    'modelId',
    'voiceId',
    'endpointingSensitivity',
    'groupMeetingMode',
}

# Valid values for enum-like fields
VALID_PROMPT_MODES = {'base', 'inject', 'replace'}
VALID_SENSITIVITY_LEVELS = {'LOW', 'MEDIUM', 'HIGH'}


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
        table_name = os.environ['NOVA_SONIC_CONFIG_TABLE_NAME']
        table = dynamodb.Table(table_name)
        
        # Extract input from AppSync event
        input_data = event['arguments']['input']
        config_id = input_data['NovaSonicConfigId']
        config_str = input_data['ConfigData']
        
        # Only allow updating the Custom config
        if config_id != 'CustomNovaSonicConfig':
            raise ValueError("Only CustomNovaSonicConfig can be updated")
        
        # Parse the JSON configuration
        try:
            config_object = json.loads(config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in ConfigData: {str(e)}")
        
        # Build item with only allowed fields
        item = {'NovaSonicConfigId': config_id}
        
        for key, value in config_object.items():
            if key in ALLOWED_FIELDS:
                # Validate enum-like fields
                if key == 'promptMode' and value not in VALID_PROMPT_MODES:
                    print(f"Invalid promptMode value: {value}, skipping")
                    continue
                if key == 'endpointingSensitivity' and value not in VALID_SENSITIVITY_LEVELS:
                    print(f"Invalid endpointingSensitivity value: {value}, skipping")
                    continue
                if key == 'groupMeetingMode':
                    # Ensure boolean
                    value = bool(value)
                item[key] = value
            else:
                print(f"Filtered out non-allowed field: {key}")
        
        # Store in DynamoDB
        table.put_item(Item=item)
        
        print(f"Successfully updated Nova Sonic config: {config_id}")
        
        return {
            'NovaSonicConfigId': config_id,
            'Success': True
        }
        
    except Exception as e:
        print(f"Error updating Nova Sonic config: {str(e)}")
        raise Exception(f"Failed to update Nova Sonic config: {str(e)}")
