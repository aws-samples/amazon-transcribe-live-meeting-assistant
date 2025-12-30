# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 4: list_meetings
List meetings from DynamoDB with filters
"""

import boto3
import os
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger()


def execute(start_date: Optional[str] = None, end_date: Optional[str] = None,
           participant: Optional[str] = None, status: str = 'ALL',
           limit: int = 20, user_id: str = None, is_admin: bool = False) -> Dict[str, Any]:
    """
    List meetings from DynamoDB with optional filters.
    Enforces user-based access control.
    
    Args:
        start_date: Optional ISO 8601 start date
        end_date: Optional ISO 8601 end date
        participant: Optional participant name filter
        status: Meeting status filter ('STARTED', 'ENDED', 'ALL')
        limit: Maximum number of meetings to return
        user_id: User ID for access control
        is_admin: Whether user is admin
    
    Returns:
        Dict with list of meetings and metadata
    """
    dynamodb = boto3.resource('dynamodb')
    table_name = os.environ.get('CALLS_TABLE')
    table = dynamodb.Table(table_name)
    
    try:
        # Query strategy depends on filters
        if start_date and end_date:
            # Use date-based query for efficiency
            meetings = query_by_date_range(table, start_date, end_date, limit * 2)
        else:
            # Scan for recent meetings
            meetings = scan_recent_meetings(table, limit * 2)
        
        # Apply UBAC filter
        if not is_admin:
            meetings = [m for m in meetings if m.get('Owner') == user_id or m.get('AgentId') == user_id]
        
        # Filter by participant if specified
        if participant:
            meetings = [m for m in meetings 
                       if participant_in_meeting(m, participant)]
        
        # Filter by status
        if status != 'ALL':
            meetings = [m for m in meetings if m.get('Status') == status]
        
        # Limit results
        meetings = meetings[:limit]
        
        # Format response
        result = {
            'meetings': [format_meeting(m) for m in meetings],
            'count': len(meetings),
            'filters': {
                'startDate': start_date,
                'endDate': end_date,
                'participant': participant,
                'status': status
            }
        }
        
        logger.info(f"Listed {len(meetings)} meetings for user {user_id}")
        return result
    
    except Exception as e:
        logger.error(f"Error listing meetings: {e}")
        raise ValueError(f"Failed to list meetings: {str(e)}")


def query_by_date_range(table, start_date: str, end_date: str, limit: int) -> List[Dict]:
    """
    Query meetings by date range using GSI.
    LMA uses DateShardIndex for efficient date-based queries.
    """
    meetings = []
    
    # Extract date for shard key
    date = start_date[:10] if start_date else datetime.utcnow().strftime('%Y-%m-%d')
    
    # LMA uses sharded date index (6 shards per day)
    for shard in range(6):
        shard_pad = f"{shard:02d}"
        pk = f"list#{date}#s#{shard_pad}"
        
        try:
            response = table.query(
                KeyConditionExpression='PK = :pk AND SK BETWEEN :start AND :end',
                ExpressionAttributeValues={
                    ':pk': pk,
                    ':start': f'ts#{start_date}',
                    ':end': f'ts#{end_date}'
                },
                Limit=limit
            )
            
            meetings.extend(response.get('Items', []))
            
            if len(meetings) >= limit:
                break
        
        except Exception as e:
            logger.warning(f"Error querying shard {shard}: {e}")
            continue
    
    return meetings


def scan_recent_meetings(table, limit: int) -> List[Dict]:
    """
    Scan for recent meetings.
    Used when no date filter specified.
    """
    try:
        response = table.scan(
            FilterExpression='begins_with(PK, :prefix) AND SK = :sk',
            ExpressionAttributeValues={
                ':prefix': 'c#',
                ':sk': 'metadata'
            },
            Limit=limit
        )
        
        return response.get('Items', [])
    
    except Exception as e:
        logger.error(f"Error scanning meetings: {e}")
        return []


def participant_in_meeting(meeting: Dict[str, Any], participant_name: str) -> bool:
    """Check if participant name is in meeting"""
    participants = meeting.get('Participants', [])
    
    # Participants may be list of names or list of dicts
    if not participants:
        return False
    
    # Handle list of strings
    if isinstance(participants[0], str):
        return any(participant_name.lower() in p.lower() for p in participants)
    
    # Handle list of dicts
    return any(participant_name.lower() in p.get('Name', '').lower() 
              for p in participants if isinstance(p, dict))


def format_meeting(meeting: Dict[str, Any]) -> Dict[str, Any]:
    """Format meeting data for response"""
    return {
        'meetingId': meeting.get('CallId', meeting.get('PK', '').replace('c#', '')),
        'meetingName': meeting.get('MeetingTopic', ''),
        'startTime': meeting.get('CreatedAt', ''),
        'endTime': meeting.get('UpdatedAt', ''),
        'duration': meeting.get('TotalConversationDurationMillis', 0) / 1000,
        'status': meeting.get('Status', 'UNKNOWN'),
        'participants': meeting.get('Participants', []),
        'owner': meeting.get('Owner', meeting.get('AgentId', '')),
        'hasSummary': bool(meeting.get('Summary')),
        'hasTranscript': bool(meeting.get('TranscriptUri') or meeting.get('RecordingUrl'))
    }