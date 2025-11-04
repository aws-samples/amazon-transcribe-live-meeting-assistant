"""
Custom Resource Lambda to deploy Lambda@Edge function in us-east-1.
This function creates, updates, and deletes the Lambda@Edge function
that validates Cognito tokens for VNC WebSocket connections.
"""
import json
import boto3
from botocore.exceptions import ClientError
import urllib.request
import zipfile
import io

# cfnresponse module for Python 3.12+
# Based on https://github.com/aws-cloudformation/custom-resource-helper-python
SUCCESS = "SUCCESS"
FAILED = "FAILED"

def send(event, context, responseStatus, responseData, physicalResourceId=None, noEcho=False, reason=None):
    """Send response to CloudFormation"""
    responseUrl = event['ResponseURL']
    
    responseBody = {
        'Status': responseStatus,
        'Reason': reason or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        'PhysicalResourceId': physicalResourceId or context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'NoEcho': noEcho,
        'Data': responseData
    }
    
    json_responseBody = json.dumps(responseBody)
    
    headers = {
        'content-type': '',
        'content-length': str(len(json_responseBody))
    }
    
    try:
        req = urllib.request.Request(
            responseUrl,
            data=json_responseBody.encode('utf-8'),
            headers=headers,
            method='PUT'
        )
        with urllib.request.urlopen(req) as response:
            print(f"Status code: {response.status}")
    except Exception as e:
        print(f"send(..) failed executing request: {e}")

# Lambda@Edge function code
EDGE_FUNCTION_CODE = '''
import json
import base64
import urllib.request
from datetime import datetime

# Cache for JWKS keys (in-memory, persists across warm starts)
jwks_cache = {}

def get_jwks(region, user_pool_id):
    """Fetch JWKS from Cognito User Pool"""
    cache_key = f"{region}:{user_pool_id}"
    if cache_key in jwks_cache:
        return jwks_cache[cache_key]
    
    jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
    
    try:
        with urllib.request.urlopen(jwks_url) as response:
            jwks = json.loads(response.read().decode())
            jwks_cache[cache_key] = jwks
            return jwks
    except Exception as e:
        print(f"Error fetching JWKS: {e}")
        return None

def decode_token(token):
    """Decode JWT token without verification (just to extract claims)"""
    try:
        # Split token into parts
        parts = token.split('.')
        if len(parts) != 3:
            return None
        
        # Decode payload (add padding if needed)
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding
        
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        print(f"Error decoding token: {e}")
        return None

def validate_token(token, region, user_pool_id, client_id):
    """Validate Cognito JWT token"""
    try:
        # Decode token to get claims
        claims = decode_token(token)
        if not claims:
            print("Failed to decode token")
            return False
        
        print(f"Token claims: {json.dumps(claims)}")
        
        # Check expiration
        exp = claims.get('exp', 0)
        if exp < datetime.utcnow().timestamp():
            print(f"Token expired: {exp}")
            return False
        
        # Check issuer
        expected_issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        if claims.get('iss') != expected_issuer:
            print(f"Invalid issuer: {claims.get('iss')}")
            return False
        
        # Check token_use (should be 'id' or 'access')
        token_use = claims.get('token_use')
        if token_use not in ['id', 'access']:
            print(f"Invalid token_use: {token_use}")
            return False
        
        # For ID tokens, check client_id
        if token_use == 'id':
            aud = claims.get('aud')
            if aud != client_id:
                print(f"Invalid audience: {aud}")
                return False
        
        # For access tokens, check client_id in claims
        if token_use == 'access':
            token_client_id = claims.get('client_id')
            if token_client_id != client_id:
                print(f"Invalid client_id: {token_client_id}")
                return False
        
        print("Token validation successful")
        return True
        
    except Exception as e:
        print(f"Token validation error: {e}")
        return False

def extract_token_from_cookies(cookies):
    """Extract Cognito token from cookies"""
    if not cookies:
        return None
    
    # Look for common Cognito cookie patterns
    cookie_names = [
        'CognitoIdentityServiceProvider',
        'idToken',
        'accessToken',
    ]
    
    for cookie in cookies:
        cookie_str = cookie.get('value', '')
        # Try to find token-like strings (JWT format: xxx.yyy.zzz)
        parts = cookie_str.split('.')
        if len(parts) == 3:
            return cookie_str
    
    return None

def lambda_handler(event, context):
    """Lambda@Edge handler for viewer request"""
    request = event['Records'][0]['cf']['request']
    uri = request.get('uri', '')
    querystring = request.get('querystring', '')
    
    print(f"Request URI: {uri}")
    print(f"Query string: {querystring}")
    
    # Only validate /vnc/* paths
    if not uri.startswith('/vnc/'):
        print("Not a VNC path, allowing through")
        return request
    
    # Extract token from query string parameter
    token = None
    if querystring:
        # Parse query string to find token parameter
        params = querystring.split('&')
        for param in params:
            if '=' in param:
                key, value = param.split('=', 1)
                if key == 'token':
                    # URL decode the token
                    import urllib.parse
                    token = urllib.parse.unquote(value)
                    break
    
    if not token:
        print("No token found in query string")
        return {
            'status': '401',
            'statusDescription': 'Unauthorized',
            'body': 'Authentication required - token parameter missing',
            'headers': {
                'content-type': [{'key': 'Content-Type', 'value': 'text/plain'}]
            }
        }
    
    # Validate token (config injected at deployment time)
    region = "REGION_PLACEHOLDER"
    user_pool_id = "USER_POOL_ID_PLACEHOLDER"
    client_id = "CLIENT_ID_PLACEHOLDER"
    
    if not validate_token(token, region, user_pool_id, client_id):
        print("Token validation failed")
        return {
            'status': '403',
            'statusDescription': 'Forbidden',
            'body': 'Invalid or expired token',
            'headers': {
                'content-type': [{'key': 'Content-Type', 'value': 'text/plain'}]
            }
        }
    
    print("Authentication successful, allowing request")
    return request
'''

def create_zip_file(code_content):
    """Create a zip file containing the Lambda function code"""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr('index.py', code_content)
    zip_buffer.seek(0)
    return zip_buffer.read()

def create_edge_function(lambda_client, iam_client, function_name, role_arn, user_pool_id, region, client_id):
    """Create Lambda@Edge function in us-east-1"""
    # Replace placeholders in code
    code = EDGE_FUNCTION_CODE.replace('REGION_PLACEHOLDER', region)
    code = code.replace('USER_POOL_ID_PLACEHOLDER', user_pool_id)
    code = code.replace('CLIENT_ID_PLACEHOLDER', client_id)
    
    # Create zip file
    zip_content = create_zip_file(code)
    
    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime='python3.12',
            Role=role_arn,
            Handler='index.lambda_handler',
            Code={'ZipFile': zip_content},
            Description='Lambda@Edge function for VNC WebSocket authentication',
            Timeout=5,
            MemorySize=128,
            Publish=True,  # Must publish for Lambda@Edge
        )
        
        # When Publish=True, the response includes Version field
        # We need to construct the versioned ARN manually
        version = response.get('Version', '1')
        function_arn = response['FunctionArn']
        
        # If the ARN doesn't already have a version, append it
        if not function_arn.split(':')[-1].isdigit():
            versioned_arn = f"{function_arn}:{version}"
        else:
            versioned_arn = function_arn
            
        print(f"Created function with version {version}, ARN: {versioned_arn}")
        return versioned_arn
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceConflictException':
            # Function already exists, update it
            return update_edge_function(lambda_client, function_name, user_pool_id, region, client_id)
        raise

def update_edge_function(lambda_client, function_name, user_pool_id, region, client_id):
    """Update existing Lambda@Edge function"""
    code = EDGE_FUNCTION_CODE.replace('REGION_PLACEHOLDER', region)
    code = code.replace('USER_POOL_ID_PLACEHOLDER', user_pool_id)
    code = code.replace('CLIENT_ID_PLACEHOLDER', client_id)
    
    # Create zip file
    zip_content = create_zip_file(code)
    
    # Update function code and publish new version
    response = lambda_client.update_function_code(
        FunctionName=function_name,
        ZipFile=zip_content,
        Publish=True,  # Must publish for Lambda@Edge
    )
    
    # Construct versioned ARN
    version = response.get('Version', '1')
    function_arn = response['FunctionArn']
    
    # If the ARN doesn't already have a version, append it
    if not function_arn.split(':')[-1].isdigit():
        versioned_arn = f"{function_arn}:{version}"
    else:
        versioned_arn = function_arn
        
    print(f"Updated function with version {version}, ARN: {versioned_arn}")
    return versioned_arn

def delete_edge_function(lambda_client, function_name):
    """Delete Lambda@Edge function"""
    try:
        # List all versions
        versions = lambda_client.list_versions_by_function(FunctionName=function_name)
        
        # Delete all versions except $LATEST
        for version in versions.get('Versions', []):
            if version['Version'] != '$LATEST':
                try:
                    lambda_client.delete_function(
                        FunctionName=function_name,
                        Qualifier=version['Version']
                    )
                except ClientError as e:
                    print(f"Error deleting version {version['Version']}: {e}")
        
        # Delete the function
        lambda_client.delete_function(FunctionName=function_name)
        print(f"Deleted function: {function_name}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            raise

def handler(event, context):
    """Custom resource handler"""
    print(f"Event: {json.dumps(event)}")
    
    response_data = {}
    physical_resource_id = event.get('PhysicalResourceId', 'EdgeAuthFunction')
    
    try:
        # Get properties
        props = event['ResourceProperties']
        function_name = props['FunctionName']
        role_arn = props['RoleArn']
        user_pool_id = props['UserPoolId']
        region = props['Region']
        client_id = props['ClientId']
        
        # Create clients for us-east-1
        lambda_client = boto3.client('lambda', region_name='us-east-1')
        iam_client = boto3.client('iam', region_name='us-east-1')
        
        if event['RequestType'] in ['Create', 'Update']:
            # Create or update function
            function_arn = create_edge_function(
                lambda_client, iam_client, function_name, 
                role_arn, user_pool_id, region, client_id
            )
            
            # Return the versioned ARN (required for Lambda@Edge)
            response_data['FunctionArn'] = function_arn
            physical_resource_id = function_arn
            
            print(f"Function ARN: {function_arn}")
            send(event, context, SUCCESS, response_data, physical_resource_id)
            
        elif event['RequestType'] == 'Delete':
            # Delete function
            delete_edge_function(lambda_client, function_name)
            send(event, context, SUCCESS, response_data, physical_resource_id)
            
    except Exception as e:
        print(f"Error: {str(e)}")
        send(event, context, FAILED, {}, physical_resource_id, reason=str(e))
