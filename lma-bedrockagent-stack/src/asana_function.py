import json
import boto3
import os
from botocore.exceptions import ClientError
import requests
from datetime import datetime, timedelta

# Initialize AWS clients
secretsmanager = boto3.client('secretsmanager')

def get_asana_credentials():
    """Retrieve Asana credentials from AWS Secrets Manager"""
    try:
        secret_name = os.environ['ASANA_SECRET_NAME']
        response = secretsmanager.get_secret_value(SecretId=secret_name)
        secret = json.loads(response['SecretString'])
        return {
            'access_token': secret['access_token'],
            'workspace_gid': secret.get('workspace_gid', None)
        }
    except Exception as e:
        print(f"Error retrieving Asana credentials: {str(e)}")
        raise e

def create_asana_task(credentials, name, notes, project_gid=None, workspace_gid=None, assignee=None, due_on=None):
    """Create a new task in Asana"""
    try:
        # Prepare headers with authentication
        headers = {
            'Authorization': f'Bearer {credentials["access_token"]}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Use provided workspace_gid or default from credentials
        if not workspace_gid:
            workspace_gid = credentials.get('workspace_gid')
            if not workspace_gid:
                raise ValueError("Workspace GID is required")
        
        # Prepare the task data
        task_data = {
            'data': {
                'name': name,
                'notes': notes,
                'workspace': workspace_gid
            }
        }
        
        # Add optional fields if provided
        if project_gid:
            task_data['data']['projects'] = [project_gid]
        
        if assignee:
            task_data['data']['assignee'] = assignee
        
        if due_on:
            task_data['data']['due_on'] = due_on
        
        # Create the task
        response = requests.post(
            "https://app.asana.com/api/1.0/tasks",
            headers=headers,
            json=task_data
        )
        response.raise_for_status()
        result = response.json()
        
        # Get the created task details
        task_gid = result.get('data', {}).get('gid')
        task_url = f"https://app.asana.com/0/{project_gid if project_gid else workspace_gid}/{task_gid}"
        
        return {
            'gid': task_gid,
            'url': task_url,
            'name': name,
            'success': True
        }
            
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {str(e)}")
        if hasattr(e, 'response') and e.response:
            print(f"Response content: {e.response.content}")
        raise e
    except Exception as e:
        print(f"Error creating Asana task: {str(e)}")
        raise e

def lambda_handler(event, context):
    print("Event: ", json.dumps(event))
    
    # Extract parameters from the event
    parameters = event.get('parameters', [])
    name = next((param['value'] for param in parameters if param['name'] == 'name'), None)
    notes = next((param['value'] for param in parameters if param['name'] == 'notes'), '')
    project_gid = next((param['value'] for param in parameters if param['name'] == 'project_gid'), None)
    workspace_gid = next((param['value'] for param in parameters if param['name'] == 'workspace_gid'), None)
    assignee = next((param['value'] for param in parameters if param['name'] == 'assignee'), None)
    due_on = next((param['value'] for param in parameters if param['name'] == 'due_on'), None)
    
    # Validate required parameters
    if not name:
        return format_response(event, "Task name is required", False)
    
    # Format due date if provided as relative date
    if due_on and due_on.lower().startswith('in '):
        try:
            days_match = re.search(r'in (\d+) days?', due_on.lower())
            if days_match:
                days = int(days_match.group(1))
                due_on = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        except:
            pass
    
    try:
        # Get Asana credentials
        credentials = get_asana_credentials()
        
        # Create the task
        result = create_asana_task(credentials, name, notes, project_gid, workspace_gid, assignee, due_on)
        
        # Format the success response
        response_message = f"Successfully created task '{name}' in Asana."
        
        return format_response(event, response_message, True, result)
        
    except Exception as e:
        error_message = f"Failed to create Asana task: {str(e)}"
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
        response_body["TEXT"]["body"] += f"\nTask URL: {data['url']}"
    
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
