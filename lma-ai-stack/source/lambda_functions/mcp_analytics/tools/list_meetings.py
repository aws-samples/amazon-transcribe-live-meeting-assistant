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
            meetings = query_by_date_range(table, start_date, end_date, limit)
        else:
            # Query recent meetings using today's date shards
            from datetime import datetime, timedelta
            today = datetime.utcnow()
            # Query last 7 days to ensure we find meetings
            start = (today - timedelta(days=7)).strftime('%Y-%m-%d')
            end = today.strftime('%Y-%m-%d')
            meetings = query_by_date_range(table, start, end, limit)
        
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
    Query meetings by date range using date-sharded list items.
    LMA uses cls#YYYY-MM-DD#s#NN pattern for efficient queries.
    """
    from datetime import datetime, timedelta
    
    meetings = []
    meeting_ids = set()  # Track unique meetings
    
    # Parse dates
    start = datetime.fromisoformat(start_date[:10]) if start_date else datetime.utcnow() - timedelta(days=7)
    end = datetime.fromisoformat(end_date[:10]) if end_date else datetime.utcnow()
    
    # Query each date in range
    current = start
    while current <= end and len(meetings) < limit:
        date_str = current.strftime('%Y-%m-%d')
        
        # LMA uses 6 shards per day (00-05)
        for shard in range(6):
            if len(meetings) >= limit:
                break
                
            shard_pad = f"{shard:02d}"
            pk = f"cls#{date_str}#s#{shard_pad}"
            
            try:
                logger.info(f"Querying list items: {pk}")
                response = table.query(
                    KeyConditionExpression='PK = :pk',
                    ExpressionAttributeValues={
                        ':pk': pk
                    },
                    Limit=limit
                )
                
                # Extract CallIds from list items
                for item in response.get('Items', []):
                    call_id = item.get('CallId')
                    if call_id and call_id not in meeting_ids:
                        meeting_ids.add(call_id)
                        # Get full meeting data
                        meeting = get_meeting_by_id(table, call_id)
                        if meeting:
                            meetings.append(meeting)
                
                logger.info(f"Found {len(meetings)} meetings so far")
                
            except Exception as e:
                logger.warning(f"Error querying {pk}: {e}")
                continue
        
        current += timedelta(days=1)
    
    return meetings[:limit]


def get_meeting_by_id(table, call_id: str) -> Optional[Dict]:
    """Get full meeting data by CallId"""
    try:
        response = table.get_item(
            Key={'PK': f'c#{call_id}', 'SK': f'c#{call_id}'}
        )
        return response.get('Item')
    except Exception as e:
        logger.warning(f"Error getting meeting {call_id}: {e}")
        return None


def scan_recent_meetings(table, limit: int) -> List[Dict]:
    """
    Scan for recent meetings.
    Used when no date filter specified.
    """
    try:
        # LMA stores meeting metadata with PK = SK = "c#{CallId}"
        logger.info(f"Scanning for meetings with limit {limit}")
        response = table.scan(
            FilterExpression='begins_with(PK, :prefix)',
            ExpressionAttributeValues={
                ':prefix': 'c#'
            },
            Limit=limit * 10  # Get more items since we'll filter
        )
        
        all_items = response.get('Items', [])
        logger.info(f"Scan returned {len(all_items)} items total")
        
        # Filter to only meeting metadata items (where PK = SK)
        meetings = [item for item in all_items if item.get('PK') == item.get('SK')]
        logger.info(f"Found {len(meetings)} meeting items after filtering")
        
        return meetings[:limit]
    
    except Exception as e:
        logger.error(f"Error scanning meetings: {e}", exc_info=True)
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