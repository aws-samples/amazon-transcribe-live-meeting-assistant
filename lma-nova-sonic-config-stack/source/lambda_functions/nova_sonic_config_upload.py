# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import boto3
import cfnresponse
import json
import os

DEFAULT_CONFIG_PK = "DefaultNovaSonicConfig"
CUSTOM_CONFIG_PK = "CustomNovaSonicConfig"

DEFAULT_CONFIG_INFO = f"""Default Nova Sonic voice assistant configuration. Do not edit - changes will be overwritten on stack updates.
To customize the voice assistant, edit the custom config item: {CUSTOM_CONFIG_PK}.
"""

CUSTOM_CONFIG_INFO = f"""Custom Nova Sonic voice assistant configuration. Attribute values here override the default config in item: {DEFAULT_CONFIG_PK}.
Changes are preserved during stack updates. Available attributes:
- systemPrompt (String): The system prompt text for the voice assistant
- promptMode (String): How to apply custom prompt - 'base' (use as-is), 'inject' (append to default), or 'replace' (fully replace default)
- modelId (String): Override the Nova model ID (optional)
"""

def get_default_config():
    """Return the default Nova Sonic configuration."""
    return {
        'NovaSonicConfigId': DEFAULT_CONFIG_PK,
        '*Information*': DEFAULT_CONFIG_INFO,
        'systemPrompt': 'You are Alex, an AI meeting assistant. Be concise and helpful.',
        'promptMode': 'base',
        'modelId': 'amazon.nova-2-sonic-v1:0',
        'description': 'Default Nova Sonic configuration. Do not edit - changes will be overwritten on stack updates.'
    }

def get_empty_custom_config():
    """Return an empty custom configuration template."""
    return {
        'NovaSonicConfigId': CUSTOM_CONFIG_PK,
        '*Information*': CUSTOM_CONFIG_INFO,
        'description': 'Custom Nova Sonic configuration. Edit attribute values here to override defaults. Changes are preserved during stack updates.'
    }

def lambda_handler(event, context):
    """
    CloudFormation custom resource handler for Nova Sonic configuration.
    Creates default and custom configuration items in DynamoDB.
    """
    print(f"Event: {json.dumps(event)}")
    
    the_event = event['RequestType']
    print(f"Request type: {the_event}")
    
    cfn_status = cfnresponse.SUCCESS
    response_data = {}
    
    try:
        if the_event in ('Create', 'Update'):
            config_table_name = event['ResourceProperties']['NovaSonicConfigTableName']
            
            dynamodb = boto3.resource('dynamodb')
            config_table = dynamodb.Table(config_table_name)
            
            # Always update default configuration (for both Create and Update)
            print(f"Populating/updating default Nova Sonic config in table: {config_table_name}")
            default_item = get_default_config()
            print(f"Writing default item to DynamoDB: {json.dumps(default_item)}")
            response = config_table.put_item(Item=default_item)
            print(f"DynamoDB response: {response}")
            
            # Only create custom config on initial Create (preserve on Update)
            if the_event == 'Create':
                print(f"Creating empty custom Nova Sonic config (for Create event): {config_table_name}")
                custom_item = get_empty_custom_config()
                print(f"Writing initial custom item to DynamoDB: {json.dumps(custom_item)}")
                response = config_table.put_item(Item=custom_item)
                print(f"DynamoDB response: {response}")
            else:
                print("Update event - preserving existing custom configuration")
        
        elif the_event == 'Delete':
            # Don't delete configuration items on stack deletion
            # This preserves custom configuration
            print("Delete event - preserving configuration items (not deleting)")
        
    except Exception as e:
        print(f"Operation failed: {str(e)}")
        response_data['Data'] = str(e)
        cfn_status = cfnresponse.FAILED
    
    print(f"Returning CloudFormation status: {cfn_status}")
    cfnresponse.send(event, context, cfn_status, response_data)
