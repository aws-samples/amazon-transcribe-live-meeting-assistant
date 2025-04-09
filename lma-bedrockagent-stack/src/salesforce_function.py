import json
import boto3
import os
from botocore.exceptions import ClientError
import requests
from datetime import datetime

# Initialize AWS clients
secretsmanager = boto3.client('secretsmanager')

def get_salesforce_credentials():
    """Retrieve Salesforce credentials from AWS Secrets Manager"""
    try:
        secret_name = os.environ['SALESFORCE_SECRET_NAME']
        response = secretsmanager.get_secret_value(SecretId=secret_name)
        secret = json.loads(response['SecretString'])
        return {
            'instance_url': secret['instance_url'],
            'access_token': secret['access_token'],
            'client_id': secret['client_id'],
            'client_secret': secret['client_secret'],
            'username': secret['username'],
            'password': secret['password'],
            'security_token': secret.get('security_token', '')
        }
    except Exception as e:
        print(f"Error retrieving Salesforce credentials: {str(e)}")
        raise e

def get_salesforce_token(credentials):
    """Get or refresh Salesforce OAuth token"""
    try:
        # If we already have a valid token, use it
        if credentials.get('access_token'):
            return credentials['access_token']
        
        # Otherwise, get a new token
        auth_url = f"https://login.salesforce.com/services/oauth2/token"
        payload = {
            'grant_type': 'password',
            'client_id': credentials['client_id'],
            'client_secret': credentials['client_secret'],
            'username': credentials['username'],
            'password': credentials['password'] + credentials.get('security_token', '')
        }
        
        response = requests.post(auth_url, data=payload)
        response.raise_for_status()
        token_data = response.json()
        
        # Update the secret with the new token
        credentials['access_token'] = token_data['access_token']
        credentials['instance_url'] = token_data['instance_url']
        
        secretsmanager.update_secret_value(
            SecretId=os.environ['SALESFORCE_SECRET_NAME'],
            SecretString=json.dumps(credentials)
        )
        
        return token_data['access_token']
    except Exception as e:
        print(f"Error getting Salesforce token: {str(e)}")
        raise e

def create_opportunity(credentials, name, amount, close_date, stage, description, account_id=None):
    """Create a new opportunity in Salesforce"""
    try:
        token = get_salesforce_token(credentials)
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        # Format the opportunity data
        opportunity_data = {
            'Name': name,
            'Amount': float(amount),
            'CloseDate': close_date,
            'StageName': stage,
            'Description': description
        }
        
        # If account_id is provided, link the opportunity to that account
        if account_id:
            opportunity_data['AccountId'] = account_id
        
        # Create the opportunity
        response = requests.post(
            f"{credentials['instance_url']}/services/data/v58.0/sobjects/Opportunity",
            headers=headers,
            json=opportunity_data
        )
        response.raise_for_status()
        result = response.json()
        
        # Get the created opportunity details
        if result.get('success') and result.get('id'):
            opportunity_id = result['id']
            opportunity_url = f"{credentials['instance_url']}/{opportunity_id}"
            return {
                'id': opportunity_id,
                'url': opportunity_url,
                'name': name,
                'success': True
            }
        else:
            raise Exception("Failed to create opportunity")
            
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {str(e)}")
        if e.response.status_code == 401:
            # Token expired, clear it and retry once
            credentials['access_token'] = None
            return create_opportunity(credentials, name, amount, close_date, stage, description, account_id)
        raise e
    except Exception as e:
        print(f"Error creating Salesforce opportunity: {str(e)}")
        raise e

def lambda_handler(event, context):
    print("Event: ", json.dumps(event))
    
    # Extract parameters from the event
    parameters = event.get('parameters', [])
    name = next((param['value'] for param in parameters if param['name'] == 'name'), None)
    amount = next((param['value'] for param in parameters if param['name'] == 'amount'), None)
    close_date = next((param['value'] for param in parameters if param['name'] == 'close_date'), None)
    stage = next((param['value'] for param in parameters if param['name'] == 'stage'), 'Prospecting')
    description = next((param['value'] for param in parameters if param['name'] == 'description'), '')
    account_id = next((param['value'] for param in parameters if param['name'] == 'account_id'), None)
    
    # Validate required parameters
    if not name:
        return format_response(event, "Opportunity name is required", False)
    
    if not amount:
        return format_response(event, "Opportunity amount is required", False)
    
    # Format close date if not provided
    if not close_date:
        close_date = datetime.now().strftime("%Y-%m-%d")
    
    try:
        # Get Salesforce credentials
        credentials = get_salesforce_credentials()
        
        # Create the opportunity
        result = create_opportunity(credentials, name, amount, close_date, stage, description, account_id)
        
        # Format the success response
        response_message = (
            f"Successfully created opportunity '{name}' for ${amount}. "
            f"Close date: {close_date}, Stage: {stage}. "
            f"Opportunity ID: {result['id']}"
        )
        
        return format_response(event, response_message, True, result)
        
    except Exception as e:
        error_message = f"Failed to create Salesforce opportunity: {str(e)}"
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
        response_body["TEXT"]["body"] += f"\nOpportunity URL: {data['url']}"
    
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
