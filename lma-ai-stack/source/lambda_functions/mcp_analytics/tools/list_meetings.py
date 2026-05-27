# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 4: list_meetings
List meetings from DynamoDB with filters
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3

from tools.url_helper import get_meeting_url, get_virtual_participant_url

logger = logging.getLogger()


def execute(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    participant: Optional[str] = None,
    status: str = "ALL",
    limit: int = 20,
    user_id: str = None,
    is_admin: bool = False,
) -> Dict[str, Any]:
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
    dynamodb = boto3.resource("dynamodb")
    table_name = os.environ.get("CALLS_TABLE")
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
            start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            end = today.strftime("%Y-%m-%d")
            meetings = query_by_date_range(table, start, end, limit)

        # Apply UBAC filter
        if not is_admin:
            meetings = [
                m for m in meetings if m.get("Owner") == user_id or m.get("AgentId") == user_id
            ]

        # Filter by participant if specified
        if participant:
            meetings = [m for m in meetings if participant_in_meeting(m, participant)]

        # Filter by status
        if status != "ALL":
            meetings = [m for m in meetings if m.get("Status") == status]

        # Limit results
        meetings = meetings[:limit]

        # Format response
        result = {
            "meetings": [format_meeting(m) for m in meetings],
            "count": len(meetings),
            "filters": {
                "startDate": start_date,
                "endDate": end_date,
                "participant": participant,
                "status": status,
            },
        }

        logger.info(f"Listed {len(meetings)} meetings for user {user_id}")
        return result

    except Exception as e:
        logger.error(f"Error listing meetings: {e}")
        raise ValueError(f"Failed to list meetings: {str(e)}")


def query_by_date_range(table, start_date: str, end_date: str, limit: int) -> List[Dict]:
    """
    Query meetings by date range using the TypeDateIndex GSI.

    The GSI is keyed by ItemType (HASH, always "call") and SK (RANGE, format
    "ts#<ISO8601>#id#<CallId>").  A single query covers the whole date range
    and paginates via LastEvaluatedKey — no shard fan-out.
    """
    from datetime import timedelta

    from boto3.dynamodb.conditions import Key

    meetings: List[Dict] = []
    meeting_ids = set()

    # Parse dates — fall back to "last 7 days" / "now" when unset.
    start_iso = (
        start_date if start_date else (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
    )
    end_iso = end_date if end_date else datetime.utcnow().isoformat() + "Z"

    # SK format is `ts#<ISO8601>#id#<CallId>`. To get an inclusive
    # date-range slice on the GSI we use:
    #   lower bound: "ts#<start_iso>"   (any SK starting with this date sorts
    #                                    at or after this point)
    #   upper bound: "ts#<end_iso>~"    ("~" / 0x7E sorts *after* every char
    #                                    that can legally follow the date in
    #                                    the SK — including "T" (0x54), which
    #                                    is the literal that appears in real
    #                                    SKs like "ts#2026-05-27T17:34:..." )
    #
    # Earlier code used "ts#<end_iso>#~" (an extra "#"), which silently
    # excluded every meeting whose SK contained a "T" right after the date,
    # because "T" (0x54) > "#" (0x23). That caused list_meetings to return
    # zero rows for any caller that didn't pass an explicit end_date past
    # midnight. Do not re-introduce the stray "#".
    sk_lo = f"ts#{start_iso}"
    sk_hi = f"ts#{end_iso}~"

    query_kwargs = {
        "IndexName": "TypeDateIndex",
        "KeyConditionExpression": (Key("ItemType").eq("call") & Key("SK").between(sk_lo, sk_hi)),
        "ScanIndexForward": False,  # newest first
        "Limit": min(limit, 100),
    }

    # Bound pagination so a very sparse / restrictive caller filter can't
    # force an unbounded scan of the GSI.  Matches the resolver's MAX_PAGES.
    max_pages = 10
    pages = 0
    last_key = None
    while len(meetings) < limit and pages < max_pages:
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key
        try:
            response = table.query(**query_kwargs)
        except Exception as e:
            logger.warning("GSI query failed: %s", e)
            break

        for item in response.get("Items", []):
            call_id = item.get("CallId")
            if not call_id or call_id in meeting_ids:
                continue
            meeting_ids.add(call_id)
            meeting = get_meeting_by_id(table, call_id)
            if meeting:
                meetings.append(meeting)
            if len(meetings) >= limit:
                break

        last_key = response.get("LastEvaluatedKey")
        pages += 1
        if not last_key:
            break

    if last_key and pages >= max_pages:
        logger.info(
            "query_by_date_range: hit MAX_PAGES=%d cap (collected=%d)", max_pages, len(meetings)
        )
    return meetings[:limit]


def get_meeting_by_id(table, call_id: str) -> Optional[Dict]:
    """Get full meeting data by CallId"""
    try:
        response = table.get_item(Key={"PK": f"c#{call_id}", "SK": f"c#{call_id}"})
        return response.get("Item")
    except Exception as e:
        logger.warning(f"Error getting meeting {call_id}: {e}")
        return None


# NOTE: `scan_recent_meetings` was removed.  The callers that used it now go
# through `query_by_date_range` (GSI-backed, O(matched items)); direct scans
# don't scale and are no longer acceptable once the tracking table grows.


def participant_in_meeting(meeting: Dict[str, Any], participant_name: str) -> bool:
    """Check if participant name is in meeting"""
    participants = meeting.get("Participants", [])

    # Participants may be list of names or list of dicts
    if not participants:
        return False

    # Handle list of strings
    if isinstance(participants[0], str):
        return any(participant_name.lower() in p.lower() for p in participants)

    # Handle list of dicts
    return any(
        participant_name.lower() in p.get("Name", "").lower()
        for p in participants
        if isinstance(p, dict)
    )


def format_meeting(meeting: Dict[str, Any]) -> Dict[str, Any]:
    """Format meeting data for response"""
    meeting_id = meeting.get("CallId", meeting.get("PK", "").replace("c#", ""))
    return {
        "meetingId": meeting_id,
        "meetingName": meeting.get("MeetingTopic", ""),
        "startTime": meeting.get("CreatedAt", ""),
        "endTime": meeting.get("UpdatedAt", ""),
        "duration": meeting.get("TotalConversationDurationMillis", 0) / 1000,
        "status": meeting.get("Status", "UNKNOWN"),
        "participants": meeting.get("Participants", []),
        "owner": meeting.get("Owner", meeting.get("AgentId", "")),
        "hasSummary": bool(meeting.get("Summary")),
        "hasTranscript": bool(meeting.get("TranscriptUri") or meeting.get("RecordingUrl")),
        "meetingUrl": get_meeting_url(meeting_id),
        "virtualParticipantUrl": get_virtual_participant_url(meeting_id),
    }
