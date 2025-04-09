import json
import boto3
import os
from botocore.exceptions import ClientError
import requests
import base64

# Initialize AWS clients
secretsmanager = boto3.client('secretsmanager')

def get_jira_credentials():
    """Retrieve Jira credentials from AWS Secrets Manager"""
    try:
        secret_name = os.environ['JIRA_SECRET_NAME']
        response = secretsmanager.get_secret_value(SecretId=secret_name)
        secret = json.loads(response['SecretString'])
        return {
            'url': secret['url'],  # e.g., https://your-domain.atlassian.net
            'email': secret['email'],
            'api_token': secret['api_token']
        }
    except Exception as e:
        print(f"Error retrieving Jira credentials: {str(e)}")
        raise e

def create_jira_issue(credentials, project_key, issue_type, summary, description, priority=None, assignee=None, labels=None):
    """Create a new issue in Jira"""
    try:
        # Prepare authentication
        auth = (credentials['email'], credentials['api_token'])
        
        # Prepare headers
        headers = {
            'Content-Type': 'application/json'
        }
        
        # Prepare the issue data
        issue_data = {
            'fields': {
                'project': {
                    'key': project_key
                },
                'summary': summary,
                'description': description,
                'issuetype': {
                    'name': issue_type
                }
            }
        }
        
        # Add optional fields if provided
        if priority:
            issue_data['fields']['priority'] = {'name': priority}
        
        if assignee:
            issue_data['fields']['assignee'] = {'name': assignee}
        
        if labels:
            if isinstance(labels, str):
                labels = [label.strip() for label in labels.split(',')]
            issue_data['fields']['labels'] = labels
        
        # Create the issue
        response = requests.post(
            f"{credentials['url']}/rest/api/2/issue",
            auth=auth,
            headers=headers,
            json=issue_data
        )
        response.raise_for_status()
        result = response.json()
        
        # Get the created issue details
        issue_key = result.get('key')
        issue_id = result.get('id')
        issue_url = f"{credentials['url']}/browse/{issue_key}"
        
        return {
            'key': issue_key,
            'id': issue_id,
            'url': issue_url,
            'success': True
        }
            
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {str(e)}")
        if hasattr(e, 'response') and e.response:
            print(f"Response content: {e.response.content}")
        raise e
    except Exception as e:
        print(f"Error creating Jira issue: {str(e)}")
        raise e

def lambda_handler(event, context):
    print("Event: ", json.dumps(event))
    
    # Extract parameters from the event
    parameters = event.get('parameters', [])
    project_key = next((param['value'] for param in parameters if param['name'] == 'project_key'), None)
    issue_type = next((param['value'] for param in parameters if param['name'] == 'issue_type'), 'Task')
    summary = next((param['value'] for param in parameters if param['name'] == 'summary'), None)
    description = next((param['value'] for param in parameters if param['name'] == 'description'), '')
    priority = next((param['value'] for param in parameters if param['name'] == 'priority'), None)
    assignee = next((param['value'] for param in parameters if param['name'] == 'assignee'), None)
    labels = next((param['value'] for param in parameters if param['name'] == 'labels'), None)
    
    # Validate required parameters
    if not project_key:
        return format_response(event, "Project key is required", False)
    
    if not summary:
        return format_response(event, "Issue summary is required", False)
    
    try:
        # Get Jira credentials
        credentials = get_jira_credentials()
        
        # Create the issue
        result = create_jira_issue(credentials, project_key, issue_type, summary, description, priority, assignee, labels)
        
        # Format the success response
        response_message = (
            f"Successfully created {issue_type} '{summary}' in project {project_key}. "
            f"Issue key: {result['key']}"
        )
        
        return format_response(event, response_message, True, result)
        
    except Exception as e:
        error_message = f"Failed to create Jira issue: {str(e)}"
        return format_response(event, error_message, False)

def format_response(event, message, success, data=None):
    """Format the response for Bedrock Agent"""
    action_group = event['actionGroup']
    function = event['function']
    
    response_body = {
        "TEXT": {
            "body": message
        }
    }
    
    if data and success:
        response_body["TEXT"]["body"] += f"\nIssue URL: {data['url']}"
    
    action_response = {
        'actionGroup': action_group,
        'function': function,
        'functionResponse': {
            'responseBody': response_body
        }
    }
    
    function_response = {'response': action_response, 'messageVersion': event['messageVersion']}
    print("Response: {}".format(function_response))
    return function_response
