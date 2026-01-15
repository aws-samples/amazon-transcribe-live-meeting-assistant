# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 2: get_meeting_transcript
Retrieve full transcript from S3 with speaker attribution
"""

import boto3
import json
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger()


def execute(meeting_id: str, format: str = 'text', user_id: str = None, 
           is_admin: bool = False) -> Dict[str, Any]:
    """
    Retrieve full meeting transcript from S3.
    Enforces user-based access control.
    
    Args:
        meeting_id: Meeting ID (CallId)
        format: Output format - 'json', 'text', or 'srt'
        user_id: User ID for access control
        is_admin: Whether user is admin
    
    Returns:
        Dict with transcript in requested format
    """
    if not meeting_id:
        raise ValueError("Meeting ID is required")
    
    # Check permissions
    if not can_access_meeting(meeting_id, user_id, is_admin):
        raise PermissionError(f"Access denied to meeting {meeting_id}")
    
    # Fetch transcript from S3
    # LMA transcript files use underscores instead of spaces in filenames
    s3 = boto3.client('s3')
    bucket = os.environ.get('RECORDINGS_BUCKET')
    prefix = os.environ.get('TRANSCRIPT_PREFIX', 'lma-transcripts/')
    
    # Convert meeting ID: spaces and hyphens to underscores (LMA filename format)
    filename_safe_id = meeting_id.replace(' ', '_').replace('-', '_').replace('/', '_').replace(':', '_')
    
    # Try multiple possible transcript file patterns
    possible_keys = [
        f"{prefix}{filename_safe_id}-TRANSCRIPT.txt",
        f"{prefix}{meeting_id}-TRANSCRIPT.txt",
        f"{prefix}{meeting_id}/transcript.json",
        f"{prefix}{filename_safe_id}/transcript.json"
    ]
    
    transcript_data = None
    key_found = None
    
    for key in possible_keys:
        try:
            logger.info(f"Trying transcript key: {key}")
            response = s3.get_object(Bucket=bucket, Key=key)
            
            # Handle both JSON and text formats
            content = response['Body'].read()
            if key.endswith('.json'):
                transcript_data = json.loads(content)
            else:
                # Text format - parse into segments
                transcript_data = {'text': content.decode('utf-8')}
            
            key_found = key
            logger.info(f"Found transcript at: {key}")
            break
            
        except s3.exceptions.NoSuchKey:
            continue
        except Exception as e:
            logger.warning(f"Error reading {key}: {e}")
            continue
    
    if not transcript_data:
        raise ValueError(f"Transcript not found for meeting {meeting_id}")
    
    # Format based on requested format
    if format == 'json':
        return {
            'meetingId': meeting_id,
            'format': 'json',
            'transcript': transcript_data
        }
    elif format == 'text':
        return {
            'meetingId': meeting_id,
            'format': 'text',
            'transcript': format_as_text(transcript_data)
        }
    elif format == 'srt':
        return {
            'meetingId': meeting_id,
            'format': 'srt',
            'transcript': format_as_srt(transcript_data)
        }
    else:
        raise ValueError(f"Invalid format: {format}. Must be 'json', 'text', or 'srt'")


def can_access_meeting(meeting_id: str, user_id: str, is_admin: bool) -> bool:
    """
    Check if user has permission to access meeting.
    Admins can access all meetings, users can access only their own.
    """
    if is_admin:
        return True
    
    # Query DynamoDB to check ownership
    dynamodb = boto3.resource('dynamodb')
    table_name = os.environ.get('CALLS_TABLE')
    table = dynamodb.Table(table_name)
    
    try:
        response = table.get_item(
            Key={'PK': f'c#{meeting_id}', 'SK': f'c#{meeting_id}'}
        )
        
        meeting = response.get('Item', {})
        owner = meeting.get('Owner', meeting.get('AgentId', ''))
        
        # Check if user is the owner
        return owner == user_id
    
    except Exception as e:
        logger.error(f"Error checking meeting access: {e}")
        return False


def format_as_text(transcript_data: Dict[str, Any]) -> str:
    """
    Format transcript as readable text with speaker attribution.
    
    Handles two formats:
    1. Plain text (already formatted): {'text': 'Speaker: content...'}
    2. JSON segments: {'segments': [...]}
    """
    # If already plain text, return as-is
    if 'text' in transcript_data and isinstance(transcript_data['text'], str):
        return transcript_data['text']
    
    # Otherwise format from segments
    lines = []
    segments = transcript_data.get('segments', transcript_data.get('Segments', []))
    
    for segment in segments:
        # Handle different transcript formats
        speaker = segment.get('SegmentSpeaker', segment.get('speaker', 'Unknown'))
        text = segment.get('SegmentText', segment.get('text', ''))
        
        # Format timestamp
        start_time = segment.get('SegmentStartTime', segment.get('startTime', 0))
        timestamp = format_timestamp(start_time)
        
        lines.append(f"[{timestamp}] {speaker}: {text}")
    
    return '\n'.join(lines)


def format_as_srt(transcript_data: Dict[str, Any]) -> str:
    """
    Format transcript as SRT subtitle format.
    
    Format:
    1
    00:00:00,000 --> 00:00:05,000
    Speaker: Text
    """
    lines = []
    segments = transcript_data.get('segments', transcript_data.get('Segments', []))
    
    for i, segment in enumerate(segments, 1):
        speaker = segment.get('SegmentSpeaker', segment.get('speaker', 'Unknown'))
        text = segment.get('SegmentText', segment.get('text', ''))
        start_time = segment.get('SegmentStartTime', segment.get('startTime', 0))
        end_time = segment.get('SegmentEndTime', segment.get('endTime', start_time + 5))
        
        # SRT format
        lines.append(str(i))
        lines.append(f"{format_srt_timestamp(start_time)} --> {format_srt_timestamp(end_time)}")
        lines.append(f"{speaker}: {text}")
        lines.append("")  # Blank line between subtitles
    
    return '\n'.join(lines)


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"