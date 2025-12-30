# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
CloudFormation custom resource response module.
This module provides the send() function for CloudFormation custom resources.
"""

import json
import urllib3
from typing import Any, Dict, Optional

http = urllib3.PoolManager()

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(event: Dict[str, Any], context: Any, responseStatus: str, 
         responseData: Dict[str, Any], physicalResourceId: Optional[str] = None,
         noEcho: bool = False, reason: Optional[str] = None) -> None:
    """
    Send response to CloudFormation custom resource.
    
    Args:
        event: The CloudFormation event
        context: Lambda context
        responseStatus: SUCCESS or FAILED
        responseData: Dict of data to return to CloudFormation
        physicalResourceId: Physical resource ID (optional)
        noEcho: Whether to mask output (optional)
        reason: Reason for failure (optional)
    """
    responseUrl = event['ResponseURL']
    
    print(f"ResponseURL: {responseUrl}")
    
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
    
    print(f"Response body: {json_responseBody}")
    
    headers = {
        'content-type': '',
        'content-length': str(len(json_responseBody))
    }
    
    try:
        response = http.request(
            'PUT',
            responseUrl,
            headers=headers,
            body=json_responseBody
        )
        print(f"Status code: {response.status}")
        
    except Exception as e:
        print(f"send(..) failed executing http.request(..): {e}")