# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 3: get_meeting_summary
Retrieve AI-generated meeting summary from DynamoDB
"""

import boto3
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger()


def execute(meeting_id: str, include_action_items: bool = True, 
           include_topics: bool = True, user_id: str = None, 
           is_admin: bool = False) -> Dict[str, Any]:
    """
    Get AI-generated meeting summary from DynamoDB.
    Enforces user-based access control.
    
    Args:
        meeting_id: Meeting ID (CallId)
        include_action_items: Include action items in response
        include_topics: Include key topics in response
        user_id: User ID for access control
        is_admin: Whether user is admin
    
    Returns:
        Dict with meeting summary, action items, and topics
    """
    if not meeting_id:
        raise ValueError("Meeting ID is required")
    
    # Check permissions
    if not can_access_meeting(meeting_id, user_id, is_admin):
        raise PermissionError(f"Access denied to meeting {meeting_id}")
    
    # Query DynamoDB for meeting data
    dynamodb = boto3.resource('dynamodb')
    table_name = os.environ.get('CALLS_TABLE')
    table = dynamodb.Table(table_name)
    
    try:
        # Get meeting metadata (LMA stores with PK = SK = "c#{CallId}")
        response = table.get_item(
            Key={'PK': f'c#{meeting_id}', 'SK': f'c#{meeting_id}'}
        )
        
        meeting = response.get('Item', {})
        
        if not meeting:
            raise ValueError(f"Meeting {meeting_id} not found")
        
        # Build response (LMA uses CallSummaryText field)
        result = {
            'meetingId': meeting_id,
            'meetingName': meeting.get('MeetingTopic', meeting.get('CallId', '')),
            'startTime': meeting.get('CreatedAt', ''),
            'endTime': meeting.get('UpdatedAt', ''),
            'duration': meeting.get('TotalConversationDurationMillis', 0) / 1000,  # Convert to seconds
            'participants': meeting.get('Participants', []),
            'owner': meeting.get('Owner', meeting.get('AgentId', '')),
            'status': meeting.get('Status', 'UNKNOWN'),
            'summary': meeting.get('CallSummaryText', meeting.get('Summary', 'No summary available'))
        }
        
        # Add action items if requested
        if include_action_items:
            result['actionItems'] = parse_action_items(meeting)
        
        # Add topics if requested
        if include_topics:
            result['topics'] = parse_topics(meeting)
        
        logger.info(f"Retrieved summary for meeting {meeting_id}")
        return result
    
    except Exception as e:
        logger.error(f"Error retrieving meeting summary: {e}")
        raise ValueError(f"Failed to retrieve summary: {str(e)}")


def can_access_meeting(meeting_id: str, user_id: str, is_admin: bool) -> bool:
    """
    Check if user has permission to access meeting.
    Admins can access all meetings, users can access only their own.
    """
    if is_admin:
        return True
    
    dynamodb = boto3.resource('dynamodb')
    table_name = os.environ.get('CALLS_TABLE')
    table = dynamodb.Table(table_name)
    
    try:
        response = table.get_item(
            Key={'PK': f'c#{meeting_id}', 'SK': f'c#{meeting_id}'}
        )
        
        meeting = response.get('Item', {})
        owner = meeting.get('Owner', meeting.get('AgentId', ''))
        
        return owner == user_id
    
    except Exception as e:
        logger.error(f"Error checking meeting access: {e}")
        return False


def parse_action_items(meeting: Dict[str, Any]) -> list:
    """
    Extract action items from meeting data.
    Action items may be in Summary field or separate ActionItems field.
    """
    # Check for explicit ActionItems field
    if 'ActionItems' in meeting:
        return meeting['ActionItems']
    
    # Try to extract from CallSummaryText or Summary field
    summary = meeting.get('CallSummaryText', meeting.get('Summary', ''))
    action_items = []
    
    # Look for action items section in summary
    if 'Action Items:' in summary or 'ACTION ITEMS:' in summary:
        lines = summary.split('\n')
        in_action_section = False
        
        for line in lines:
            if 'Action Items:' in line or 'ACTION ITEMS:' in line:
                in_action_section = True
                continue
            
            if in_action_section:
                # Stop at next section
                if line.strip() and line.strip().endswith(':'):
                    break
                
                # Add action item
                if line.strip() and (line.strip().startswith('-') or line.strip().startswith('•')):
                    action_items.append(line.strip().lstrip('-•').strip())
    
    return action_items


def parse_topics(meeting: Dict[str, Any]) -> list:
    """
    Extract key topics from meeting data.
    Topics may be in Summary field or separate Topics field.
    """
    # Check for explicit Topics field
    if 'Topics' in meeting:
        return meeting['Topics']
    
    # Try to extract from CallSummaryText or Summary field
    summary = meeting.get('CallSummaryText', meeting.get('Summary', ''))
    topics = []
    
    # Look for topics/key points section in summary
    if 'Key Topics:' in summary or 'Topics:' in summary or 'KEY POINTS:' in summary:
        lines = summary.split('\n')
        in_topics_section = False
        
        for line in lines:
            if any(marker in line for marker in ['Key Topics:', 'Topics:', 'KEY POINTS:']):
                in_topics_section = True
                continue
            
            if in_topics_section:
                # Stop at next section
                if line.strip() and line.strip().endswith(':'):
                    break
                
                # Add topic
                if line.strip() and (line.strip().startswith('-') or line.strip().startswith('•')):
                    topics.append(line.strip().lstrip('-•').strip())
    
    return topics