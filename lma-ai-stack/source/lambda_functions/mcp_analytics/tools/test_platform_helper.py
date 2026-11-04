# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Platform and meeting ID normalisation for start_meeting_now / schedule_meeting."""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools.platform_helper import normalize_meeting_id, normalize_platform  # noqa: E402


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

    def test_a_meet_meeting_id_that_is_not_meet_is_refused(self):
        for value in (
            "96187501703",
            "https://zoom.us/j/1",
            "https://meet.google.com.example.org/abc-defg-hij",
            "",
        ):
            with self.assertRaises(ValueError, msg=value):
                normalize_meeting_id("GOOGLE_MEET", value)


if __name__ == "__main__":
    unittest.main()
