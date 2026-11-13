# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Platform and meeting ID normalisation for start_meeting_now / schedule_meeting."""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools.platform_helper import (  # noqa: E402
    ZOOM_LINK_NOT_READ,
    ZOOM_PASSWORD_LOOKS_LIKE_LINK_VALUE,
    check_zoom_meeting,
    is_complete_zoom_link_token,
    normalize_meeting_id,
    normalize_platform,
    zoom_link_parts,
)

# The table of Meet inputs every implementation of the Meet code rule runs.
SHARED_MEET_CASES = HERE.parents[4] / "test-fixtures" / "google-meet-inputs.json"


class TestNormalizePlatform(unittest.TestCase):
    def test_the_documented_names_are_accepted_in_any_case(self):
        for value, expected in (
            ("Zoom", "ZOOM"),
            ("teams", "TEAMS"),
            ("CHIME", "CHIME"),
            ("Webex", "WEBEX"),
            ("Google Meet", "GOOGLE_MEET"),
            ("google_meet", "GOOGLE_MEET"),
            ("GOOGLE-MEET", "GOOGLE_MEET"),
            ("googlemeet", "GOOGLE_MEET"),
            ("Meet", "GOOGLE_MEET"),
        ):
            self.assertEqual(normalize_platform(value), expected, value)

    def test_an_unknown_platform_names_the_choices(self):
        for value in ("Skype", "", None, "google"):
            with self.assertRaises(ValueError) as raised:
                normalize_platform(value)
            self.assertIn("Google Meet", str(raised.exception))


class TestNormalizeMeetingId(unittest.TestCase):
    def test_other_platforms_only_lose_whitespace(self):
        self.assertEqual(normalize_meeting_id("ZOOM", "961 8750 1703"), "96187501703")
        self.assertEqual(
            normalize_meeting_id("WEBEX", " https://x.webex.com/j.php?MTID=a "),
            "https://x.webex.com/j.php?MTID=a",
        )

    def test_a_meet_code_or_link_becomes_the_code(self):
        for value in (
            "abc-defg-hij",
            "ABCDEFGHIJ",
            "https://meet.google.com/abc-defg-hij?authuser=0",
            "meet.google.com/meet/abc-defg-hij",
        ):
            self.assertEqual(normalize_meeting_id("GOOGLE_MEET", value), "abc-defg-hij", value)

    def test_each_shared_meet_input_becomes_the_tables_code_or_is_refused(self):
        cases = json.loads(SHARED_MEET_CASES.read_text())["cases"]
        self.assertGreater(len(cases), 20)
        for case in cases:
            if case["code"] is None:
                with self.assertRaises(ValueError, msg=case["input"]):
                    normalize_meeting_id("GOOGLE_MEET", case["input"])
            else:
                self.assertEqual(
                    normalize_meeting_id("GOOGLE_MEET", case["input"]), case["code"], case["input"]
                )

    def test_a_meet_meeting_id_that_is_not_meet_is_refused(self):
        for value in (
            "96187501703",
            "https://zoom.us/j/1",
            "https://meet.google.com.example.org/abc-defg-hij",
            "",
        ):
            with self.assertRaises(ValueError, msg=value):
                normalize_meeting_id("GOOGLE_MEET", value)


# A made-up value of the shape of a whole Zoom link pwd value: 30 letters and
# digits, then ".1".
TOKEN = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0.1"


class TestZoomLinkToken(unittest.TestCase):
    def test_a_whole_link_pwd_value_is_complete(self):
        self.assertEqual(len(TOKEN), 32)
        self.assertTrue(is_complete_zoom_link_token(TOKEN))
        self.assertTrue(is_complete_zoom_link_token(f" {TOKEN} "))

    def test_a_cut_off_or_other_value_is_not(self):
        for value in (TOKEN[:20], TOKEN[:-2], TOKEN[:-1], TOKEN[2:], "02061967", "", None):
            self.assertFalse(is_complete_zoom_link_token(value), value)


class TestZoomLinkParts(unittest.TestCase):
    def test_the_join_link_forms_give_number_and_pwd(self):
        self.assertEqual(
            zoom_link_parts(f"https://us05web.zoom.us/j/81234567890?pwd={TOKEN}"),
            ("81234567890", TOKEN),
        )
        self.assertEqual(zoom_link_parts("https://zoom.us/j/81234567890"), ("81234567890", None))
        self.assertEqual(
            zoom_link_parts(f"https://app.zoom.us/wc/81234567890/join?pwd={TOKEN}"),
            ("81234567890", TOKEN),
        )
        self.assertEqual(
            zoom_link_parts("https://zoom.us/wc/join/81234567890"), ("81234567890", None)
        )

    def test_other_values_are_not_read(self):
        for value in (
            "81234567890",
            "https://zoom.us/my/someone",
            "https://zoom.us.example.com/j/81234567890",
            "https://teams.microsoft.com/meet/123?p=abc",
            "",
            None,
        ):
            self.assertIsNone(zoom_link_parts(value), value)


class TestCheckZoomMeeting(unittest.TestCase):
    def test_a_passcode_is_accepted_with_or_without_grouping_spaces(self):
        for password in ("02061967", "0206 1967", "1234 5678 90", "aB3x9", "ab@-_*1234", "", None):
            check_zoom_meeting("81234567890", password)

    def test_a_complete_link_pwd_value_is_accepted(self):
        check_zoom_meeting("81234567890", TOKEN)

    def test_a_long_password_that_is_not_a_complete_link_value_is_refused(self):
        for password in (TOKEN[:20], TOKEN[:-2], "abcdefghijk"):
            with self.assertRaises(ValueError, msg=password) as raised:
                check_zoom_meeting("81234567890", password)
            self.assertEqual(str(raised.exception), ZOOM_PASSWORD_LOOKS_LIKE_LINK_VALUE)

    def test_a_join_link_is_accepted_and_its_pwd_covers_a_cut_off_password(self):
        link = f"https://us05web.zoom.us/j/81234567890?pwd={TOKEN}"
        check_zoom_meeting(link, None)
        check_zoom_meeting(link, "02061967")
        check_zoom_meeting(link, TOKEN[:20])
        with self.assertRaises(ValueError):
            check_zoom_meeting("https://zoom.us/j/81234567890", TOKEN[:20])

    def test_a_link_the_virtual_participant_cannot_read_is_refused(self):
        for meeting_id in ("https://zoom.us/my/someone", "https://example.com/j/81234567890"):
            with self.assertRaises(ValueError, msg=meeting_id) as raised:
                check_zoom_meeting(meeting_id, None)
            self.assertEqual(str(raised.exception), ZOOM_LINK_NOT_READ)

    def test_a_zoom_link_passes_normalisation_intact(self):
        link = f" https://us05web.zoom.us/j/81234567890?pwd={TOKEN} "
        self.assertEqual(normalize_meeting_id("ZOOM", link), link.strip())


if __name__ == "__main__":
    unittest.main()
