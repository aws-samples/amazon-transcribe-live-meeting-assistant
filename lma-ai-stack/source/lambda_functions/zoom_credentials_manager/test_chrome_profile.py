# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""
Unit tests for the per-platform Chromium profile handling in the Zoom
credentials manager Lambda.

Covers platform normalization, the per-user/per-platform S3 prefix layout, and
the platform-scoped status/delete operations. The actual S3 client is patched
so no AWS calls are made.
"""

import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch

# Set env before import (module builds boto3 clients and reads config at import).
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["VP_PROFILES_BUCKET"] = "test-vp-profiles-bucket"
os.environ["LMA_STACK_NAME"] = "lma-test"

import index

VALID_SUB = "12345678-1234-1234-1234-123456789abc"


def make_event(field_name, sub=VALID_SUB, arguments=None):
    """Create an AppSync resolver event with a Cognito identity."""
    return {
        "info": {"fieldName": field_name},
        "identity": {"sub": sub, "claims": {"sub": sub}},
        "arguments": arguments or {},
    }


def user_hash(sub):
    return hashlib.sha256(sub.lower().encode("utf-8")).hexdigest()


class TestNormalizePlatform(unittest.TestCase):
    """_normalize_platform must mirror the TS normalizePlatform()."""

    def test_known_platforms(self):
        cases = {
            "ZOOM": "zoom",
            "Zoom": "zoom",
            "zoom": "zoom",
            "CHIME": "chime",
            "Chime": "chime",
            "TEAMS": "teams",
            "Teams": "teams",
            "team": "teams",
            "WEBEX": "webex",
            "Webex": "webex",
            "GOOGLE_MEET": "googlemeet",
        }
        for raw, expected in cases.items():
            self.assertEqual(index._normalize_platform(raw), expected, raw)

    def test_empty_and_none_fall_back_to_unknown(self):
        self.assertEqual(index._normalize_platform(""), "unknown")
        self.assertEqual(index._normalize_platform(None), "unknown")
        self.assertEqual(index._normalize_platform("   "), "unknown")

    def test_garbage_is_sanitized(self):
        # Non-alphanumeric chars stripped; mirrors the TS fallback.
        self.assertEqual(index._normalize_platform("weird!!"), "weird")


class TestUserProfilePrefix(unittest.TestCase):
    """Per-user S3 key layout (one profile per user, shared across platforms)."""

    def test_prefix_without_platform_targets_all(self):
        prefix = index._user_profile_prefix(VALID_SUB)
        self.assertEqual(prefix, f"profiles/{user_hash(VALID_SUB)}/")

    def test_prefix_ignores_platform(self):
        prefix = index._user_profile_prefix(VALID_SUB, "WEBEX")
        self.assertEqual(prefix, f"profiles/{user_hash(VALID_SUB)}/")

    def test_prefix_same_regardless_of_platform(self):
        self.assertEqual(
            index._user_profile_prefix(VALID_SUB, "Zoom"),
            index._user_profile_prefix(VALID_SUB, "teams"),
        )

    def test_sub_hash_is_case_insensitive(self):
        a = index._user_profile_prefix(VALID_SUB.upper(), "teams")
        b = index._user_profile_prefix(VALID_SUB.lower(), "teams")
        self.assertEqual(a, b)


class TestGetChromeProfileStatus(unittest.TestCase):
    """getMyChromeProfileStatus passes the optional platform through."""

    @patch.object(index, "s3")
    def test_status_ignores_platform(self, mock_s3):
        mock_s3.list_objects_v2.return_value = {"Contents": []}
        index.get_my_chrome_profile_status(
            make_event("getMyChromeProfileStatus", arguments={"platform": "WEBEX"})
        )
        _, kwargs = mock_s3.list_objects_v2.call_args
        self.assertEqual(kwargs["Prefix"], f"profiles/{user_hash(VALID_SUB)}/")

    @patch.object(index, "s3")
    def test_status_without_platform_uses_aggregate_prefix(self, mock_s3):
        mock_s3.list_objects_v2.return_value = {"Contents": []}
        index.get_my_chrome_profile_status(make_event("getMyChromeProfileStatus"))
        _, kwargs = mock_s3.list_objects_v2.call_args
        self.assertEqual(kwargs["Prefix"], f"profiles/{user_hash(VALID_SUB)}/")


class TestDeleteChromeProfile(unittest.TestCase):
    """deleteMyChromeProfile wipes the single per-user profile."""

    @patch.object(index, "s3")
    def test_delete_ignores_platform(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = paginator

        result = index.delete_my_chrome_profile(
            make_event("deleteMyChromeProfile", arguments={"platform": "TEAMS"})
        )
        self.assertTrue(result)
        _, kwargs = paginator.paginate.call_args
        self.assertEqual(kwargs["Prefix"], f"profiles/{user_hash(VALID_SUB)}/")

    @patch.object(index, "s3")
    def test_delete_without_platform_wipes_all(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = paginator

        index.delete_my_chrome_profile(make_event("deleteMyChromeProfile"))
        _, kwargs = paginator.paginate.call_args
        self.assertEqual(kwargs["Prefix"], f"profiles/{user_hash(VALID_SUB)}/")

    @patch.object(index, "s3")
    def test_delete_counts_objects_across_pages(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a"}, {"Key": "b"}]},
            {"Contents": [{"Key": "c"}]},
        ]
        mock_s3.get_paginator.return_value = paginator

        deleted = index._delete_user_profiles(VALID_SUB, "webex")
        self.assertEqual(deleted, 3)
        self.assertEqual(mock_s3.delete_objects.call_count, 2)


class TestDeleteZoomCredentialsLeavesProfile(unittest.TestCase):
    """Removing Zoom credentials deletes only the secret, not the profile."""

    @patch.object(index, "secrets")
    @patch.object(index, "s3")
    def test_profile_not_touched(self, mock_s3, mock_secrets):
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = paginator

        index.delete_my_zoom_credentials(make_event("deleteMyZoomCredentials"))

        mock_secrets.delete_secret.assert_called_once()
        paginator.paginate.assert_not_called()
        mock_s3.delete_objects.assert_not_called()


if __name__ == "__main__":
    unittest.main()
