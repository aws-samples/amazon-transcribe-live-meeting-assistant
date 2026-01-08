# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 6: start_meeting_now
Start a meeting immediately with virtual participant
"""

import boto3
import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import uuid

logger = logging.getLogger()

def execute(
    meeting_name: str,
    meeting_platform: str,
    meeting_id: str,
    meeting_password: Optional[str] = None,
    user_id: str = None,
    is_admin: bool = False
) -> Dict[str, Any]:
    """
    Start a meeting immediately with virtual participant.
    
    Args:
        meeting_name: Name/title of the meeting
        meeting_platform: Platform (Zoom, Teams, Chime, Webex)
        meeting_id: Meeting ID or URL
        meeting_password: Optional meeting password
        user_id: User ID for access control
        is_admin: Whether user is admin
    
    Returns:
        Dict with virtual participant details and status
    """
    if not meeting_name or not meeting_platform or not meeting_id:
        raise ValueError("meeting_name, meeting_platform, and meeting_id are required")
    
    # Validate and normalize platform (VP code expects uppercase)
    valid_platforms = {
        'zoom': 'ZOOM',
        'teams': 'TEAMS',
        'chime': 'CHIME',
        'webex': 'WEBEX'
    }
    
    platform_lower = meeting_platform.lower()
    if platform_lower not in valid_platforms:
        raise ValueError(f"Invalid platform. Must be one of: Zoom, Teams, Chime, Webex")
    
    # Convert to uppercase for VP infrastructure
    meeting_platform = valid_platforms[platform_lower]
    
    # Create virtual participant via GraphQL mutation
    appsync_url = os.environ.get('APPSYNC_GRAPHQL_URL')
    if not appsync_url:
        raise ValueError("APPSYNC_GRAPHQL_URL environment variable not set")
    
    # Generate unique VP ID
    vp_id = str(uuid.uuid4())
    
    # Schedule for 5 seconds in the future (workaround: VP infrastructure only launches scheduled VPs)
    scheduled_dt = datetime.now(timezone.utc) + timedelta(seconds=5)
    current_timestamp = int(scheduled_dt.timestamp())
    
    # Prepare GraphQL mutation
    mutation = """
    mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
        createVirtualParticipant(input: $input) {
            id
            meetingName
            meetingPlatform
            meetingId
            meetingPassword
            status
            owner
            createdAt
            CallId
            vncEndpoint
            vncPort
        }
    }
    """
    
    variables = {
        "input": {
            "meetingName": meeting_name,
            "meetingPlatform": meeting_platform,
            "meetingId": meeting_id,
            "meetingPassword": meeting_password or "",
            "meetingTime": current_timestamp,
            "isScheduled": True,
            "status": "SCHEDULED"
        }
    }
    
    # Execute GraphQL mutation using boto3
    try:
        # Use IAM auth to call AppSync
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        import requests
        
        session = boto3.Session()
        credentials = session.get_credentials()
        
        headers = {
            'Content-Type': 'application/json'
        }
        
        request_body = json.dumps({
            'query': mutation,
            'variables': variables
        })
        
        request = AWSRequest(
            method='POST',
            url=appsync_url,
            data=request_body,
            headers=headers
        )
        
        SigV4Auth(credentials, 'appsync', session.region_name).add_auth(request)
        
        response = requests.post(
            appsync_url,
            headers=dict(request.headers),
            data=request_body
        )
        
        response.raise_for_status()
        result = response.json()
        
        if 'errors' in result:
            logger.error(f"GraphQL errors: {result['errors']}")
            raise ValueError(f"Failed to start meeting: {result['errors'][0]['message']}")
        
        vp_data = result['data']['createVirtualParticipant']
        
        # Build response
        response_data = {
            "virtualParticipantId": vp_data['id'],
            "meetingName": vp_data['meetingName'],
            "meetingPlatform": vp_data['meetingPlatform'],
            "meetingId": vp_data['meetingId'],
            "status": vp_data['status'],
            "owner": vp_data.get('owner', user_id),
            "message": "Virtual participant scheduled to join in 5 seconds. Check LMA UI for live status."
        }
        
        # Add CallId if available
        if vp_data.get('CallId'):
            response_data['callId'] = vp_data['CallId']
        
        # Add VNC details if available
        if vp_data.get('vncEndpoint'):
            response_data['vncEndpoint'] = vp_data['vncEndpoint']
            response_data['vncPort'] = vp_data.get('vncPort')
            response_data['vncInfo'] = "VNC preview available - check LMA UI for live view"
        
        return response_data
        
    except Exception as e:
        logger.error(f"Error starting meeting: {e}", exc_info=True)
        raise ValueError(f"Failed to start meeting: {str(e)}")