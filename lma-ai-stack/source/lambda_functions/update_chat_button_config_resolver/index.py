"""
Lambda function resolver for updateChatButtonConfig mutation
Implements AF-12 Mass Assignment security fix

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""
import json
import re
import boto3
import os
from typing import Dict, Any

dynamodb = boto3.resource('dynamodb')

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AppSync Lambda resolver for updateChatButtonConfig
    
    Implements security fix for AF-12 (Mass Assignment vulnerability)
    by filtering input to only allow button configuration fields.
    
    Args:
        event: AppSync event with arguments and identity
        context: Lambda context
        
    Returns:
        Dict with ChatButtonConfigId and Success status
    """
    try:
        # Get table name from environment
        table_name = os.environ['CHAT_BUTTON_CONFIG_TABLE_NAME']
        table = dynamodb.Table(table_name)
        
        # Extract input from AppSync event
        input_data = event['arguments']['input']
        chat_button_config_id = input_data['ChatButtonConfigId']
        button_config_str = input_data['ButtonConfig']
        
        # Parse the JSON configuration
        try:
            config_object = json.loads(button_config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in ButtonConfig: {str(e)}")
        
        # Security: Allowlist only button fields (N#LABEL format)
        # This prevents mass assignment of unexpected DynamoDB attributes
        # Pattern matches: "1#Action Items", "2#Summary", etc.
        button_pattern = re.compile(r'^\d+#')
        
        # Build item with only allowed fields
        item = {'ChatButtonConfigId': chat_button_config_id}
        
        for key, value in config_object.items():
            if button_pattern.match(key):
                item[key] = value
            else:
                print(f"Filtered out non-button field: {key}")
        
        # Store in DynamoDB
        table.put_item(Item=item)
        
        print(f"Successfully updated chat button config: {chat_button_config_id}")
        
        return {
            'ChatButtonConfigId': chat_button_config_id,
            'Success': True
        }
        
    except Exception as e:
        print(f"Error updating chat button config: {str(e)}")
        raise Exception(f"Failed to update chat button config: {str(e)}")
