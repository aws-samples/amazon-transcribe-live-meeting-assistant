# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import json
import logging
import os
import re
from datetime import datetime, timedelta

import boto3
from bedrock_compat import INVITATION_PARSER_MAX_TOKENS, build_inference_config, extract_text
from botocore.config import Config

# pylint: disable=import-error
from lma_config import LANGUAGE_MODELS, read_setting  # configuration layer

# pylint: enable=import-error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
#
# The model is the summaries' one: the Amazon Bedrock Models section's value on
# the Configuration page, else the shipped default in setting_defaults.json. The
# literal below is used when the section cannot be read, and is the same value
# as that shipped default.
FALLBACK_MODEL_ID = "global.anthropic.claude-sonnet-5-5"
BEDROCK_REGION = os.environ.get(
    "BEDROCK_REGION_OVERRIDE", os.environ.get("AWS_REGION", "us-east-1")
)
BEDROCK_ENDPOINT_URL = os.environ.get(
    "BEDROCK_ENDPOINT_URL", f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
)

# Initialize Bedrock client
bedrock = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION,
    endpoint_url=BEDROCK_ENDPOINT_URL,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


def get_generated_text(response):
    """Extract generated text from Bedrock response, excluding any reasoning block"""
    return extract_text(response)


def call_bedrock(prompt_data):
    """Call Bedrock to parse meeting invitation"""
    modelId = read_setting(
        LANGUAGE_MODELS.name,
        "summaryModelId",
        default=FALLBACK_MODEL_ID,
    )
    logger.info(f"Bedrock request - ModelId: {modelId}")

    message = {"role": "user", "content": [{"text": prompt_data}]}

    response = bedrock.converse(
        modelId=modelId,
        messages=[message],
        inferenceConfig=build_inference_config(modelId, INVITATION_PARSER_MAX_TOKENS),
    )

    generated_text = get_generated_text(response)
    # The response holds what was parsed from the invitation, so only its size is logged.
    logger.info("Bedrock response: %d characters", len(generated_text or ""))
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
- agenda: The agenda the invitation lists, as a single string with one item per line, in the order given. Only items the invitation actually lists as an agenda (or as topics, items to discuss, or similar); use null if it lists none. Do NOT make up agenda items from the meeting title or anything else.

CRITICAL MEETING ID AND PASSWORD RULES:
1. If information is not available or cannot be determined, use null for that field
2. For meetingPlatform, use one of: ZOOM, TEAMS, CHIME, WEBEX, GOOGLE_MEET
3. Extract meeting ID from URLs when possible (e.g., Zoom meeting ID from zoom.us URLs)
4. For Zoom, extract the numeric meeting ID, not the full URL. Remove any spaces from the meeting ID (e.g., "961 8750 1703" should become "96187501703")
5. For Teams, extract the numeric meeting ID and the passcode separately, NOT the full URL.
   - New format URLs look like https://teams.microsoft.com/meet/MEETINGID?p=PASSCODE
     where the meetingId is ONLY the numeric part (e.g. "243574196567966") and the
     meetingPassword is the value of the "p" parameter (e.g. "ixpjsDWNj8cLxyHlQD").
   - For older "meetup-join" URLs (https://teams.microsoft.com/l/meetup-join/...) where there
     is no separate numeric ID or passcode, use the full join URL as the meetingId.
6. For Webex, there are two URL styles:
   - Personal-room URLs look like https://meetXXXX.webex.com/meet/prYYYYYYYYYY. For these,
     extract ONLY the numeric meeting ID (YYYYYYYYYY) WITHOUT the "pr" prefix. For example,
     from https://meet1648.webex.com/meet/pr2552362251, the meetingId should be "2552362251"
     (not "pr2552362251").
   - "j.php" launch URLs look like https://SITE.webex.com/PATH/j.php?MTID=<token> (and sometimes
     include a "&password=" or "&pwd=" parameter). The MTID is an opaque token, NOT a numeric
     meeting number, and the numeric ID/password usually cannot be derived from it. For these,
     set meetingId to the COMPLETE, UNMODIFIED join URL (keep the full https://... string), and
     only set meetingPassword if an explicit "password"/"pwd" parameter is present in the URL.
7. For Zoom passwords from URLs: Extract the COMPLETE password from the URL's pwd parameter. The password can contain letters, numbers, and special characters including periods (.). Extract everything after "pwd=" up to the next "&" or end of URL. Do NOT truncate the password at periods or other special characters.
8. For Google Meet (links on meet.google.com), set meetingId to the meeting code, three, four and
   three letters separated by hyphens (e.g. "abc-defg-hij" from https://meet.google.com/abc-defg-hij).
   Google Meet meetings have no password: set meetingPassword to null, and do NOT use the dial-in
   PIN that Meet invitations list under "Join by phone".

DATE AND TIME HANDLING:
9. If NO specific date or time is mentioned in the invitation, set meetingDate and meetingTime to null
10. If the meeting is recurring (contains words like "every", "weekly", "occurs", "recurring"), set isRecurring to true
11. For recurring meetings, DO NOT use the start date from the invitation if it's in the past
12. Instead, calculate the NEXT occurrence based on the recurrence pattern
13. For "every Wednesday" meetings, find the next Wednesday from today
14. For "weekly" meetings, find the next occurrence based on the day of the week mentioned
15. ALWAYS ensure the final date is today or in the future
16. If today matches the recurring day, use today's date only if the time is in the future
17. If you cannot determine a specific date or time with confidence, leave those fields as null
18. Do NOT guess or make up dates/times - only extract what is clearly stated

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
            return {"success": True, "data": parsed_data}
        except json.JSONDecodeError:
            # If JSON parsing fails, try to extract JSON from the response
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                try:
                    parsed_data = json.loads(json_match.group())
                    return {"success": True, "data": parsed_data}
                except json.JSONDecodeError:
                    pass

            return {
                "success": False,
                "error": "Failed to parse AI response as JSON",
                "raw_response": response,
            }

    except Exception as e:
        logger.error(f"Error parsing meeting invitation: {str(e)}")
        return {"success": False, "error": str(e)}


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


# Same bound as the createVirtualParticipant and updateCallAgenda resolvers, so an
# agenda this fills into the form is one the form can submit.
MAX_AGENDA_CHARS = 10000


def clean_agenda(agenda):
    """The extracted agenda as text, one item per line, or None.

    Asked for as one string, but a model asked for "the agenda items" will
    sometimes answer with a list, which is the same thing in another shape.
    Anything else is dropped rather than filled into the form as its repr.

    Cut to the limit rather than refused, unlike the form and the MCP tools:
    this only pre-fills a field the user sees and can edit before submitting,
    and refusing would lose the rest of the parsed invitation with it.
    """
    if isinstance(agenda, list):
        agenda = "\n".join(str(item).strip() for item in agenda if str(item).strip())
    if not isinstance(agenda, str) or not agenda.strip():
        return None
    return agenda.strip()[:MAX_AGENDA_CHARS]


_MEET_CODE = re.compile(r"^([a-z]{3})-?([a-z]{4})-?([a-z]{3})$")
_MEET_LINK = re.compile(
    r"^(?:https?://)?meet\.google\.com/(?:meet/)?([a-z]{3}-?[a-z]{4}-?[a-z]{3})/?(?:[?#].*)?$"
)
# Any link on meet.google.com, whether or not its path is a meeting code.
_MEET_ANY_LINK = re.compile(r"^(?:https?://)?meet\.google\.com/\S*$")

# Why a Meet invitation's link was not put in the meeting ID field. Shown by the
# paste-invitation dialog alongside the filled-in form.
MEET_NICKNAME_NOTE = (
    "The invitation's Google Meet link (meet.google.com/lookup/...) uses a meeting "
    "nickname, which only signed-in members of the organiser's Google Workspace can "
    "open, and the Virtual Participant joins as a guest. Enter the meeting code "
    "(for example abc-defg-hij) instead."
)
MEET_NOT_A_CODE_NOTE = (
    "The invitation's Google Meet details do not include a meeting code. Enter the "
    "meeting code (for example abc-defg-hij) or a https://meet.google.com/ link to it."
)


def normalize_google_meet_id(value):
    """Reduce a Google Meet code or link to its code, or return None if it is neither.

    The same rule as parseGoogleMeetMeeting in the Virtual Participant
    (google-meet-page.ts), the create form, the SDK and the meeting assistant's
    tools; each runs the shared table in test-fixtures/google-meet-inputs.json.
    A bare code in any case, with or without hyphens, or a meet.google.com link
    to one (including Meet's /meet/<code> redirect form) gives "abc-defg-hij".
    Any other meet.google.com link, such as a /lookup/ nickname link, gives None.
    """
    if not isinstance(value, str):
        return None
    text = "".join(value.split()).lower()
    if not text:
        return None
    link = _MEET_LINK.match(text)
    code = _MEET_CODE.match(link.group(1) if link else text)
    if code:
        return f"{code.group(1)}-{code.group(2)}-{code.group(3)}"
    return None


def is_google_meet_link(value):
    """Whether a value is a link on meet.google.com, whatever its path."""
    if not isinstance(value, str):
        return False
    return bool(_MEET_ANY_LINK.match("".join(value.split()).lower()))


def validate_parsed_data(data):
    """Validate and clean parsed meeting data"""
    if not isinstance(data, dict):
        return data

    data["agenda"] = clean_agenda(data.get("agenda"))

    # Remove spaces from meeting ID (common in formatted meeting IDs like "961 8750 1703")
    if data.get("meetingId"):
        data["meetingId"] = data["meetingId"].replace(" ", "")

    # Validate platform
    valid_platforms = ["ZOOM", "TEAMS", "CHIME", "WEBEX", "GOOGLE_MEET"]
    # A Meet link or code identifies the platform by itself. Checked when the model
    # gave no platform as well: the UI otherwise falls back to its own ZOOM default
    # and hands the user a Zoom form holding a Meet link.
    if data.get("meetingPlatform") not in valid_platforms and (
        normalize_google_meet_id(data.get("meetingId"))
        or is_google_meet_link(data.get("meetingId"))
    ):
        data["meetingPlatform"] = "GOOGLE_MEET"
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
        zoom_id_match = re.search(r"(\d{9,11})", meeting_id)
        if zoom_id_match:
            data["meetingId"] = zoom_id_match.group(1)

    # Clean up meeting ID for Webex.
    # Personal-room URLs look like https://meetXXXX.webex.com/meet/prYYYYYYYYYY and the
    #   meeting ID should be ONLY the numeric part (YYYYYYYYYY) without the "pr" prefix.
    # "j.php" launch URLs look like https://SITE.webex.com/PATH/j.php?MTID=<token>. The MTID
    #   is an opaque token, not a numeric meeting number, so we keep the full join URL as the
    #   meetingId and let the virtual participant navigate directly to it.
    if data.get("meetingPlatform") == "WEBEX" and data.get("meetingId"):
        meeting_id = data["meetingId"]
        if re.search(r"/j\.php\b", meeting_id, re.IGNORECASE):
            # Launch URL: preserve the complete URL untouched.
            data["meetingId"] = meeting_id.strip()
            # Pull the password from the URL if present and not already captured.
            if not data.get("meetingPassword"):
                webex_pwd_match = re.search(
                    r"[?&](?:password|pwd)=([^&\s]+)", meeting_id, re.IGNORECASE
                )
                if webex_pwd_match:
                    data["meetingPassword"] = webex_pwd_match.group(1)
        else:
            # Personal-room URL or bare ID: extract the numeric part, dropping "pr".
            webex_url_match = re.search(r"webex\.com/meet/(?:pr)?(\d+)", meeting_id, re.IGNORECASE)
            if webex_url_match:
                data["meetingId"] = webex_url_match.group(1)
            else:
                # If it starts with "pr" followed by digits, remove the "pr" prefix
                webex_pr_match = re.match(r"^pr(\d+)$", meeting_id, re.IGNORECASE)
                if webex_pr_match:
                    data["meetingId"] = webex_pr_match.group(1)

    # Google Meet: store the meeting code ("abc-defg-hij") rather than the link, as
    # the create form does, and no password. A Meet invitation's only number is the
    # dial-in PIN, which is not a meeting password. A Meet ID that is not a
    # meeting code is left out, with the reason in meetingIdNote, rather than
    # filling the form with an ID the form refuses to submit.
    if data.get("meetingPlatform") == "GOOGLE_MEET":
        data["meetingPassword"] = None
        original_id = data.get("meetingId")
        meet_id = normalize_google_meet_id(original_id)
        if meet_id:
            data["meetingId"] = meet_id
        elif original_id:
            data["meetingId"] = None
            lowered = "".join(str(original_id).split()).lower()
            data["meetingIdNote"] = (
                MEET_NICKNAME_NOTE if "meet.google.com/lookup/" in lowered else MEET_NOT_A_CODE_NOTE
            )

    # Clean up meeting ID for Teams.
    # New format: https://teams.microsoft.com/meet/243574196567966?p=ixpjsDWNj8cLxyHlQD
    #   -> meetingId="243574196567966", meetingPassword="ixpjsDWNj8cLxyHlQD"
    # Older "meetup-join" URLs have no separate numeric ID/passcode, so the full URL is kept.
    if data.get("meetingPlatform") == "TEAMS" and data.get("meetingId"):
        meeting_id = data["meetingId"]
        teams_new_match = re.search(
            r"teams\.microsoft\.com/(?:v2/.*?)?meet/(\d+)", meeting_id, re.IGNORECASE
        )
        if teams_new_match:
            data["meetingId"] = teams_new_match.group(1)
            # Extract the passcode from the "p" query parameter if not already set
            if not data.get("meetingPassword"):
                password_match = re.search(r"[?&]p=([^&\s]+)", meeting_id)
                if password_match:
                    data["meetingPassword"] = password_match.group(1)

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


# Platforms the Virtual Participant has a handler for, kept in step with the
# platform switch in the Virtual Participant's index.ts. A platform the parser
# can name but the Virtual Participant cannot join is refused up front, rather
# than auto-filling the create form with a platform that fails at launch with
# "Unsupported meeting platform".
VP_SUPPORTED_PLATFORMS = ["ZOOM", "TEAMS", "CHIME", "WEBEX", "GOOGLE_MEET"]

# What to tell the user for a recognised platform that is not supported. Empty
# while every platform the prompt names has a handler.
UNSUPPORTED_PLATFORM_MESSAGES: dict = {}


def handler(event, context):
    """Lambda handler for parsing meeting invitations"""
    invitation_text = (event.get("arguments") or {}).get("invitationText") or ""
    # The invitation text can carry meeting passcodes, so only its size is logged.
    logger.info("Received invitation to parse: %d characters", len(invitation_text))

    try:
        if not invitation_text or not invitation_text.strip():
            return json.dumps({"success": False, "error": "Meeting invitation text is required"})

        # Parse the meeting invitation
        result = parse_meeting_invitation(invitation_text)
        platform = None

        if result["success"]:
            # Validate and clean the parsed data
            result["data"] = validate_parsed_data(result["data"])

            # Reject a platform the Virtual Participant has no handler for, rather
            # than auto-filling the create form with it: the launch would fail with
            # "Unsupported meeting platform" after the user had filled everything
            # else in. The message reaches the paste-invitation dialog directly.
            platform = result["data"].get("meetingPlatform")
            if platform and platform not in VP_SUPPORTED_PLATFORMS:
                logger.info("Rejecting unsupported meeting platform: %s", platform)
                return json.dumps(
                    {
                        "success": False,
                        "error": UNSUPPORTED_PLATFORM_MESSAGES.get(
                            platform,
                            f"The Virtual Participant cannot join {platform} meetings.",
                        ),
                    }
                )

        # The parsed fields can include a meeting passcode, so the result is
        # logged by outcome, platform and the names of the fields it filled in.
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        logger.info(
            "Returning result: success=%s, platform=%s, fields=%s",
            result.get("success"),
            platform,
            sorted(k for k, v in data.items() if v),
        )
        return json.dumps(result)

    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return json.dumps({"success": False, "error": f"Internal error: {str(e)}"})
