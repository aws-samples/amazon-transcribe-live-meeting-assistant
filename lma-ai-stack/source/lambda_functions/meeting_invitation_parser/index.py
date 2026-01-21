# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import os
import json
import boto3
from botocore.config import Config
import re
from datetime import datetime, timedelta
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION_OVERRIDE", os.environ.get("AWS_REGION", "us-east-1"))
BEDROCK_ENDPOINT_URL = os.environ.get("BEDROCK_ENDPOINT_URL", f'https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com')

# Initialize Bedrock client
bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name=BEDROCK_REGION, 
    endpoint_url=BEDROCK_ENDPOINT_URL,
    config=Config(retries={'max_attempts': 50, 'mode': 'adaptive'})
)

def get_generated_text(response):
    """Extract generated text from Bedrock response"""
    return response["output"]["message"]["content"][0]["text"]

def call_bedrock(prompt_data):
    """Call Bedrock to parse meeting invitation"""
    modelId = BEDROCK_MODEL_ID
    logger.info(f"Bedrock request - ModelId: {modelId}")
    
    message = {
        "role": "user",
        "content": [{"text": prompt_data}]
    }

    response = bedrock.converse(
        modelId=modelId, 
        messages=[message],
        inferenceConfig={
            "maxTokens": 1024,
            "temperature": 0
        }
    )
    
    generated_text = get_generated_text(response)
    logger.info(f"Bedrock response: {generated_text}")
    return generated_text

def create_parsing_prompt(meeting_invitation_text):
    """Create a prompt for parsing meeting invitation"""
    current_date = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M")
    current_year = datetime.now().year
    
    prompt = f"""
You are a meeting invitation parser. Extract the following information from the meeting invitation text and return it as a JSON object with these exact keys:

- meetingName: The title/subject of the meeting
- meetingPlatform: The platform (ZOOM, TEAMS, CHIME, WEBEX, GOOGLE_MEET) - if not explicitly mentioned, try to infer from URLs or context
- meetingId: The meeting ID, room number, or join URL
- meetingPassword: The meeting password or passcode (if provided)
- meetingDate: The date in YYYY-MM-DD format (if provided/Optional)
- meetingTime: The time in HH:MM format (24-hour, if provided/Optional)
- timezone: The timezone (if provided)
- isRecurring: Boolean indicating if this is a recurring meeting
- recurrencePattern: Description of recurrence (e.g., "weekly", "every Wednesday") if recurring

CRITICAL MEETING ID RULES:
1. If information is not available or cannot be determined, use null for that field
2. For meetingPlatform, use one of: ZOOM, TEAMS, CHIME, WEBEX, GOOGLE_MEET
3. Extract meeting ID from URLs when possible (e.g., Zoom meeting ID from zoom.us URLs)
4. For Zoom, extract the numeric meeting ID, not the full URL. Remove any spaces from the meeting ID (e.g., "961 8750 1703" should become "96187501703")
5. For Teams, extract the meeting URL or conference ID
6. For Webex, extract ONLY the numeric meeting ID from URLs. Webex URLs look like https://meetXXXX.webex.com/meet/prYYYYYYYYYY where the meeting ID is ONLY the numeric part (YYYYYYYYYY) WITHOUT the "pr" prefix. For example, from https://meet1648.webex.com/meet/pr2552362251, the meetingId should be "2552362251" (not "pr2552362251")
7. IMPORTANT: Always remove spaces from meeting IDs. Meeting IDs should be continuous strings without spaces (e.g., "96187501703" not "961 8750 1703")

DATE AND TIME HANDLING:
7. If NO specific date or time is mentioned in the invitation, set meetingDate and meetingTime to null
8. If the meeting is recurring (contains words like "every", "weekly", "occurs", "recurring"), set isRecurring to true
9. For recurring meetings, DO NOT use the start date from the invitation if it's in the past
10. Instead, calculate the NEXT occurrence based on the recurrence pattern
11. For "every Wednesday" meetings, find the next Wednesday from today
12. For "weekly" meetings, find the next occurrence based on the day of the week mentioned
13. ALWAYS ensure the final date is today or in the future
14. If today matches the recurring day, use today's date only if the time is in the future
15. If you cannot determine a specific date or time with confidence, leave those fields as null
16. Do NOT guess or make up dates/times - only extract what is clearly stated

CURRENT CONTEXT:
- Today's date: {current_date}
- Current time: {current_time}
- Current year: {current_year}

Meeting invitation text:
{meeting_invitation_text}

JSON Response:
"""
    return prompt

def parse_meeting_invitation(invitation_text):
    """Parse meeting invitation using Bedrock"""
    try:
        prompt = create_parsing_prompt(invitation_text)
        response = call_bedrock(prompt)
        
        # Try to parse the JSON response
        try:
            parsed_data = json.loads(response)
            return {
                "success": True,
                "data": parsed_data
            }
        except json.JSONDecodeError:
            # If JSON parsing fails, try to extract JSON from the response
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                try:
                    parsed_data = json.loads(json_match.group())
                    return {
                        "success": True,
                        "data": parsed_data
                    }
                except json.JSONDecodeError:
                    pass
            
            return {
                "success": False,
                "error": "Failed to parse AI response as JSON",
                "raw_response": response
            }
            
    except Exception as e:
        logger.error(f"Error parsing meeting invitation: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def calculate_next_occurrence(data):
    """Calculate next occurrence for recurring meetings"""
    if not data.get("isRecurring") or not data.get("meetingTime"):
        return data
    
    try:
        current_time = datetime.now()
        meeting_time_str = data["meetingTime"]
        
        # Parse the meeting time
        meeting_time = datetime.strptime(meeting_time_str, "%H:%M").time()
        
        # Handle recurring patterns
        recurrence = data.get("recurrencePattern", "").lower()
        
        if "wednesday" in recurrence or "every wednesday" in recurrence:
            # Find next Wednesday
            days_ahead = 2 - current_time.weekday()  # Wednesday is 2
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            
            next_date = current_time + timedelta(days=days_ahead)
            
            # If it's Wednesday today, check if the meeting time is in the future
            if current_time.weekday() == 2:  # Today is Wednesday
                today_meeting = datetime.combine(current_time.date(), meeting_time)
                if today_meeting > current_time:
                    next_date = current_time  # Use today
            
            data["meetingDate"] = next_date.strftime("%Y-%m-%d")
            
        elif "weekly" in recurrence:
            # For generic weekly meetings, try to find the next occurrence
            # Default to next week same day if we can't determine the specific day
            next_date = current_time + timedelta(days=7)
            data["meetingDate"] = next_date.strftime("%Y-%m-%d")
            
        elif data.get("meetingDate"):
            # If we have a specific date, ensure it's in the future
            try:
                parsed_date = datetime.strptime(data["meetingDate"], "%Y-%m-%d")
                meeting_datetime = datetime.combine(parsed_date.date(), meeting_time)
                
                if meeting_datetime <= current_time:
                    # Move to next week/occurrence
                    if "weekly" in recurrence or "every" in recurrence:
                        next_date = parsed_date + timedelta(days=7)
                        data["meetingDate"] = next_date.strftime("%Y-%m-%d")
                    else:
                        # For non-recurring past dates, move to current year
                        next_date = parsed_date.replace(year=current_time.year)
                        if datetime.combine(next_date.date(), meeting_time) <= current_time:
                            next_date = next_date.replace(year=current_time.year + 1)
                        data["meetingDate"] = next_date.strftime("%Y-%m-%d")
                        
            except ValueError:
                # If date parsing fails, set to null
                data["meetingDate"] = None
                
    except Exception as e:
        logger.error(f"Error calculating next occurrence: {str(e)}")
        # Don't fail the entire parsing, just log the error
    
    return data

def validate_parsed_data(data):
    """Validate and clean parsed meeting data"""
    if not isinstance(data, dict):
        return data
    
    # Remove spaces from meeting ID (common in formatted meeting IDs like "961 8750 1703")
    if data.get("meetingId"):
        data["meetingId"] = data["meetingId"].replace(" ", "")
    
    # Validate platform
    valid_platforms = ["ZOOM", "TEAMS", "CHIME", "WEBEX", "GOOGLE_MEET"]
    if data.get("meetingPlatform") and data["meetingPlatform"] not in valid_platforms:
        # Try to infer platform from meeting ID or URL
        meeting_id = data.get("meetingId", "").lower()
        if "zoom" in meeting_id:
            data["meetingPlatform"] = "ZOOM"
        elif "teams" in meeting_id or "microsoft" in meeting_id:
            data["meetingPlatform"] = "TEAMS"
        elif "chime" in meeting_id:
            data["meetingPlatform"] = "CHIME"
        elif "webex" in meeting_id:
            data["meetingPlatform"] = "WEBEX"
        elif "meet.google" in meeting_id:
            data["meetingPlatform"] = "GOOGLE_MEET"
        else:
            data["meetingPlatform"] = "ZOOM"  # Default to Zoom
    
    # Clean up meeting ID for Zoom (extract numeric ID from URL)
    if data.get("meetingPlatform") == "ZOOM" and data.get("meetingId"):
        meeting_id = data["meetingId"]
        # Extract numeric meeting ID from Zoom URL
        zoom_id_match = re.search(r'(\d{9,11})', meeting_id)
        if zoom_id_match:
            data["meetingId"] = zoom_id_match.group(1)
    
    # Clean up meeting ID for Webex (extract numeric ID, remove "pr" prefix)
    # Webex URLs look like: https://meetXXXX.webex.com/meet/prYYYYYYYYYY
    # The meeting ID should be ONLY the numeric part (YYYYYYYYYY) without "pr"
    if data.get("meetingPlatform") == "WEBEX" and data.get("meetingId"):
        meeting_id = data["meetingId"]
        # If it's a full URL, extract the path part after /meet/
        webex_url_match = re.search(r'webex\.com/meet/(?:pr)?(\d+)', meeting_id, re.IGNORECASE)
        if webex_url_match:
            data["meetingId"] = webex_url_match.group(1)
        else:
            # If it starts with "pr" followed by digits, remove the "pr" prefix
            webex_pr_match = re.match(r'^pr(\d+)$', meeting_id, re.IGNORECASE)
            if webex_pr_match:
                data["meetingId"] = webex_pr_match.group(1)
    
    # Calculate next occurrence for recurring meetings
    data = calculate_next_occurrence(data)
    
    # Validate date format
    if data.get("meetingDate"):
        try:
            datetime.strptime(data["meetingDate"], "%Y-%m-%d")
        except ValueError:
            data["meetingDate"] = None
    
    # Validate time format
    if data.get("meetingTime"):
        try:
            datetime.strptime(data["meetingTime"], "%H:%M")
        except ValueError:
            data["meetingTime"] = None
    
    return data

def handler(event, context):
    """Lambda handler for parsing meeting invitations"""
    logger.info(f"Received event: {json.dumps(event)}")
    
    try:
        # Extract meeting invitation text from the event
        invitation_text = event.get("arguments", {}).get("invitationText", "")
        
        if not invitation_text or not invitation_text.strip():
            return json.dumps({
                "success": False,
                "error": "Meeting invitation text is required"
            })
        
        # Parse the meeting invitation
        result = parse_meeting_invitation(invitation_text)
        
        if result["success"]:
            # Validate and clean the parsed data
            result["data"] = validate_parsed_data(result["data"])
        
        logger.info(f"Returning result: {json.dumps(result)}")
        return json.dumps(result)
        
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Internal error: {str(e)}"
        })
