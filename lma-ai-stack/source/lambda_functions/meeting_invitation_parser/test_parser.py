# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""
Unit tests for the meeting invitation parser Lambda.

Focuses on validate_parsed_data — the pure post-processing step that cleans the
meeting ID / password the LLM extracts. Bedrock is never called here (these
tests exercise deterministic regex/cleanup logic only).
"""

import json
import os
import unittest

# Set env before import so the module-level Bedrock client construction is cheap
# and deterministic (the client is built but never invoked in these tests).
os.environ.setdefault("AWS_REGION", "us-east-1")

import index


class TestWebexMeetingId(unittest.TestCase):
    """Webex meeting-ID / password extraction in validate_parsed_data."""

    def test_jphp_launch_url_is_preserved(self):
        """A j.php?MTID launch link must be kept intact as the meeting ID.

        The MTID is an opaque token, not a numeric meeting number, so the full
        URL is the only thing the virtual participant can navigate to.
        """
        url = "https://amazon.webex.com/amazon-en/j.php?MTID=me6d2028ce7e787c6eacc187f8f738e6a"
        out = index.validate_parsed_data({"meetingPlatform": "WEBEX", "meetingId": url})
        self.assertEqual(out["meetingId"], url)
        # No explicit password parameter -> no password invented.
        self.assertFalse(out.get("meetingPassword"))

    def test_jphp_launch_url_with_whitespace_is_stripped(self):
        url = "https://amazon.webex.com/amazon-en/j.php?MTID=abc123"
        out = index.validate_parsed_data({"meetingPlatform": "WEBEX", "meetingId": f"  {url}  "})
        self.assertEqual(out["meetingId"], url)

    def test_jphp_url_password_param_extracted(self):
        url = "https://amazon.webex.com/amazon-en/j.php?MTID=abc&password=Secret123"
        out = index.validate_parsed_data({"meetingPlatform": "WEBEX", "meetingId": url})
        self.assertEqual(out["meetingId"], url)
        self.assertEqual(out["meetingPassword"], "Secret123")

    def test_jphp_url_pwd_param_extracted(self):
        url = "https://example.webex.com/x/j.php?MTID=abc&pwd=Hunter2"
        out = index.validate_parsed_data({"meetingPlatform": "WEBEX", "meetingId": url})
        self.assertEqual(out["meetingPassword"], "Hunter2")

    def test_jphp_url_does_not_overwrite_existing_password(self):
        url = "https://example.webex.com/x/j.php?MTID=abc&password=FromUrl"
        out = index.validate_parsed_data(
            {"meetingPlatform": "WEBEX", "meetingId": url, "meetingPassword": "Existing"}
        )
        self.assertEqual(out["meetingPassword"], "Existing")

    def test_personal_room_url_extracts_numeric_id(self):
        out = index.validate_parsed_data(
            {
                "meetingPlatform": "WEBEX",
                "meetingId": "https://meet1648.webex.com/meet/pr2552362251",
            }
        )
        self.assertEqual(out["meetingId"], "2552362251")

    def test_personal_room_url_without_pr_prefix(self):
        out = index.validate_parsed_data(
            {"meetingPlatform": "WEBEX", "meetingId": "https://meet1648.webex.com/meet/2552362251"}
        )
        self.assertEqual(out["meetingId"], "2552362251")

    def test_bare_pr_id_strips_prefix(self):
        out = index.validate_parsed_data({"meetingPlatform": "WEBEX", "meetingId": "pr2552362251"})
        self.assertEqual(out["meetingId"], "2552362251")


class TestMeetingIdGeneralCleanup(unittest.TestCase):
    """Cross-platform cleanup that also affects the value space above."""

    def test_spaces_removed_from_meeting_id(self):
        out = index.validate_parsed_data({"meetingPlatform": "ZOOM", "meetingId": "961 8750 1703"})
        self.assertEqual(out["meetingId"], "96187501703")

    def test_zoom_url_extracts_numeric_id(self):
        out = index.validate_parsed_data(
            {"meetingPlatform": "ZOOM", "meetingId": "https://zoom.us/j/96187501703"}
        )
        self.assertEqual(out["meetingId"], "96187501703")

    def test_teams_new_format_extracts_id_and_passcode(self):
        out = index.validate_parsed_data(
            {
                "meetingPlatform": "TEAMS",
                "meetingId": "https://teams.microsoft.com/meet/243574196567966?p=ixpjsDWNj8cLxyHlQD",
            }
        )
        self.assertEqual(out["meetingId"], "243574196567966")
        self.assertEqual(out["meetingPassword"], "ixpjsDWNj8cLxyHlQD")


if __name__ == "__main__":
    unittest.main()


class TestUnsupportedPlatformRejection(unittest.TestCase):
    """The handler must not auto-fill the form with a platform that cannot launch.

    Before this, a pasted Google Meet invitation parsed "successfully", populated
    the create form with GOOGLE_MEET and then failed at launch with "Unsupported
    meeting platform" — after the user had reviewed and submitted everything else.
    See GitHub #661.
    """

    def test_supported_platforms_are_the_ones_with_handlers(self):
        # Kept in step with the platform switch in the Virtual Participant's
        # index.ts. GOOGLE_MEET must stay out until a handler exists.
        self.assertEqual(
            sorted(index.VP_SUPPORTED_PLATFORMS), ["CHIME", "TEAMS", "WEBEX", "ZOOM"]
        )

    def test_google_meet_is_still_recognized_by_the_prompt(self):
        """Rejecting Meet requires identifying it first.

        Dropping it from the prompt's platform list would make a Meet invitation
        fall through to the "default to Zoom" branch, which is worse: the launch
        would fail for a reason that has nothing to do with Meet.
        """
        prompt = index.create_parsing_prompt("Join at https://meet.google.com/abc-defg-hij")
        self.assertIn("GOOGLE_MEET", prompt)

    def test_a_meet_invitation_is_rejected_with_an_actionable_message(self):
        original = index.parse_meeting_invitation
        index.parse_meeting_invitation = lambda _text: {
            "success": True,
            "data": {
                "meetingName": "Design review",
                "meetingPlatform": "GOOGLE_MEET",
                "meetingId": "https://meet.google.com/abc-defg-hij",
            },
        }
        try:
            result = json.loads(
                index.handler({"arguments": {"invitationText": "Join at meet.google.com"}}, None)
            )
        finally:
            index.parse_meeting_invitation = original

        self.assertFalse(result["success"])
        # The message has to tell the user what to do instead, not just say no.
        self.assertIn("Google Meet", result["error"])
        self.assertIn("Chrome extension", result["error"])
        self.assertNotIn("data", result)

    def test_a_supported_platform_still_parses(self):
        original = index.parse_meeting_invitation
        index.parse_meeting_invitation = lambda _text: {
            "success": True,
            "data": {
                "meetingName": "Standup",
                "meetingPlatform": "ZOOM",
                "meetingId": "961 8750 1703",
            },
        }
        try:
            result = json.loads(
                index.handler({"arguments": {"invitationText": "Join Zoom"}}, None)
            )
        finally:
            index.parse_meeting_invitation = original

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["meetingPlatform"], "ZOOM")
        # ...and the existing cleanup still runs on it.
        self.assertEqual(result["data"]["meetingId"], "96187501703")
