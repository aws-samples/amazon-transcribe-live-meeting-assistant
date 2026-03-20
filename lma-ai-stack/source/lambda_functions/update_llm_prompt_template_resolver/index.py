"""
Lambda function resolver for updateLLMPromptTemplate mutation
Implements input validation and security filtering

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
    AppSync Lambda resolver for updateLLMPromptTemplate
    
    Validates and filters input to only allow prompt template fields
    matching the N#LABEL pattern (e.g., 1#SUMMARY, 2#DETAILS).
    
    Args:
        event: AppSync event with arguments and identity
        context: Lambda context
        
    Returns:
        Dict with LLMPromptTemplateId and Success status
    """
    try:
        # Get table name from environment
        table_name = os.environ['LLM_PROMPT_TEMPLATE_TABLE_NAME']
        table = dynamodb.Table(table_name)
        
        # Extract input from AppSync event
        input_data = event['arguments']['input']
        template_id = input_data['LLMPromptTemplateId']
        template_config_str = input_data['TemplateConfig']
        
        # Only allow updating the Custom templates
        if template_id != 'CustomSummaryPromptTemplates':
            raise ValueError("Only CustomSummaryPromptTemplates can be updated")
        
        # Parse the JSON configuration
        try:
            config_object = json.loads(template_config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in TemplateConfig: {str(e)}")
        
        # Security: Allowlist only prompt template fields (N#LABEL format)
        # This prevents mass assignment of unexpected DynamoDB attributes
        # Pattern matches: "1#SUMMARY", "2#DETAILS", "3#ACTIONS", etc.
        template_pattern = re.compile(r'^\d+#')
        
        # Build item with only allowed fields
        item = {'LLMPromptTemplateId': template_id}
        
        for key, value in config_object.items():
            if template_pattern.match(key):
                item[key] = value
            else:
                print(f"Filtered out non-template field: {key}")
        
        # Store in DynamoDB
        table.put_item(Item=item)
        
        print(f"Successfully updated LLM prompt template: {template_id}")
        
        return {
            'LLMPromptTemplateId': template_id,
            'Success': True
        }
        
    except Exception as e:
        print(f"Error updating LLM prompt template: {str(e)}")
        raise Exception(f"Failed to update LLM prompt template: {str(e)}")
