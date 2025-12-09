"""
Custom Resource to Get AppSync Event API DNS

CloudFormation doesn't expose the DNS attribute of AWS::AppSync::Api,
so we need to call the API to get it after creation.
"""

import boto3
import cfnresponse
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

appsync_client = boto3.client('appsync')


def handler(event, context):
    """
    Custom resource handler to get Event API DNS
    """
    logger.info(f"Event: {event}")
    
    try:
        request_type = event['RequestType']
        
        if request_type == 'Delete':
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return
        
        # Get API ID from properties
        api_id = event['ResourceProperties']['ApiId']
        
        logger.info(f"Getting DNS for Event API: {api_id}")
        
        # Get API details
        response = appsync_client.get_api(apiId=api_id)
        
        # Extract DNS
        dns = response['api']['dns']
        realtime_dns = dns['REALTIME']
        http_dns = dns['HTTP']
        
        logger.info(f"REALTIME DNS: {realtime_dns}")
        logger.info(f"HTTP DNS: {http_dns}")
        
        # Construct WebSocket URL
        websocket_url = f"wss://{realtime_dns}/event"
        http_url = f"https://{http_dns}"
        
        # Return data
        response_data = {
            'RealtimeDns': realtime_dns,
            'HttpDns': http_dns,
            'WebSocketUrl': websocket_url,
            'HttpUrl': http_url
        }
        
        logger.info(f"Response data: {response_data}")
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, reason=str(e))