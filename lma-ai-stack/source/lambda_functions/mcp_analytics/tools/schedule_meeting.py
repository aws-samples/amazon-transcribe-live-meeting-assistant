# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 5: schedule_meeting
Schedule a future meeting with virtual participant
"""

import boto3
import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import uuid

logger = logging.getLogger()

def execute(
    meeting_name: str,
    meeting_platform: str,
    meeting_id: str,
    scheduled_time: str,
    meeting_password: Optional[str] = None,
    user_id: str = None,
    is_admin: bool = False
) -> Dict[str, Any]:
    """
    Schedule a future meeting with virtual participant.
    
    Args:
        meeting_name: Name/title of the meeting
        meeting_platform: Platform (Zoom, Teams, Chime, Webex)
        meeting_id: Meeting ID (numeric ID only, not URL)
        scheduled_time: ISO 8601 datetime when meeting should start
        meeting_password: Optional meeting password
        user_id: User ID for access control
        is_admin: Whether user is admin
    
    Returns:
        Dict with scheduled virtual participant details
    """
    if not meeting_name or not meeting_platform or not meeting_id or not scheduled_time:
        raise ValueError("meeting_name, meeting_platform, meeting_id, and scheduled_time are required")
    
    # Remove ALL whitespace from meeting_id (including internal spaces)
    meeting_id = ''.join(meeting_id.split())
    
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
    
    # Validate and parse scheduled time
    try:
        scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00'))
        if scheduled_dt <= datetime.now(timezone.utc):
            raise ValueError("Scheduled time must be in the future")
        scheduled_timestamp = int(scheduled_dt.timestamp())
    except Exception as e:
        raise ValueError(f"Invalid scheduled_time format. Use ISO 8601 format (e.g., 2026-01-10T15:30:00Z): {e}")
    
    # Create virtual participant via GraphQL mutation
    appsync_url = os.environ.get('APPSYNC_GRAPHQL_URL')
    if not appsync_url:
        raise ValueError("APPSYNC_GRAPHQL_URL environment variable not set")
    
    # Generate unique VP ID
    vp_id = str(uuid.uuid4())
    current_time = datetime.now(timezone.utc).isoformat()
    
    # Prepare GraphQL mutation
    mutation = """
    mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
        createVirtualParticipant(input: $input) {
            id
            meetingName
            meetingPlatform
            meetingId
            meetingPassword
            scheduledFor
            isScheduled
            status
            owner
            createdAt
        }
    }
    """
    
    variables = {
        "input": {
            "meetingName": meeting_name,
            "meetingPlatform": meeting_platform,
            "meetingId": meeting_id,
            "meetingPassword": meeting_password or "",
            "meetingTime": scheduled_timestamp,
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
            raise ValueError(f"Failed to schedule meeting: {result['errors'][0]['message']}")
        
        vp_data = result['data']['createVirtualParticipant']
        
        return {
            "virtualParticipantId": vp_data['id'],
            "meetingName": vp_data['meetingName'],
            "meetingPlatform": vp_data['meetingPlatform'],
            "meetingId": vp_data['meetingId'],
            "scheduledFor": scheduled_time,
            "status": vp_data['status'],
            "owner": vp_data.get('owner', user_id),
            "message": f"Meeting scheduled successfully for {scheduled_time}. Virtual participant will join automatically."
        }
        
    except Exception as e:
        logger.error(f"Error scheduling meeting: {e}", exc_info=True)
        raise ValueError(f"Failed to schedule meeting: {str(e)}")