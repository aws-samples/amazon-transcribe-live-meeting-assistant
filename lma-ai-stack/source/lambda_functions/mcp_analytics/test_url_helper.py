"""Tests for the LMA web app URL helper.

Loads `tools/url_helper.py` directly via importlib so this file is
independent of any sys.modules state set up by other test files.
"""

import importlib.util
import os
import unittest

_HELPER_PATH = os.path.join(os.path.dirname(__file__), "tools", "url_helper.py")


def _load_url_helper():
    spec = importlib.util.spec_from_file_location("_test_url_helper_module", _HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestUrlHelper(unittest.TestCase):
    """Test url_helper functions."""

    @classmethod
    def setUpClass(cls):
        cls.url_helper = _load_url_helper()

    def setUp(self):
        os.environ["LMA_WEB_APP_URL"] = "https://test-lma.example.com"

    def test_get_web_app_url_strips_trailing_slash(self):
        os.environ["LMA_WEB_APP_URL"] = "https://test-lma.example.com/"
        self.assertEqual(self.url_helper.get_web_app_url(), "https://test-lma.example.com")

    def test_get_meeting_url_format(self):
        self.assertEqual(
            self.url_helper.get_meeting_url("abc-123"),
            "https://test-lma.example.com/#/calls/abc-123",
        )

    def test_get_virtual_participant_url_format(self):
        self.assertEqual(
            self.url_helper.get_virtual_participant_url("vp-42"),
            "https://test-lma.example.com/#/virtual-participant/vp-42",
        )

    def test_returns_none_when_url_unset(self):
        os.environ.pop("LMA_WEB_APP_URL", None)
        self.assertIsNone(self.url_helper.get_web_app_url())
        self.assertIsNone(self.url_helper.get_meeting_url("abc"))
        self.assertIsNone(self.url_helper.get_virtual_participant_url("vp"))

    def test_returns_none_when_meeting_id_empty(self):
        self.assertIsNone(self.url_helper.get_meeting_url(""))
        self.assertIsNone(self.url_helper.get_virtual_participant_url(""))

    def test_meeting_id_special_chars_are_url_encoded(self):
        # Real meeting IDs can contain spaces, colons, and other reserved chars.
        meeting_id = "loadtest-lt-20260514T175117 - 2026-05-14T17:51:33"
        self.assertEqual(
            self.url_helper.get_meeting_url(meeting_id),
            "https://test-lma.example.com/#/calls/"
            "loadtest-lt-20260514T175117%20-%202026-05-14T17%3A51%3A33",
        )
        self.assertEqual(
            self.url_helper.get_virtual_participant_url(meeting_id),
            "https://test-lma.example.com/#/virtual-participant/"
            "loadtest-lt-20260514T175117%20-%202026-05-14T17%3A51%3A33",
        )


if __name__ == "__main__":
    unittest.main()
