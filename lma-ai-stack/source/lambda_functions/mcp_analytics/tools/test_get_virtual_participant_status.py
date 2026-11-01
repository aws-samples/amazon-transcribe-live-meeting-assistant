# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Who get_virtual_participant_status answers: the owner, an entry of the
Virtual Participant's SharedWith, and Admins."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools import get_virtual_participant_status  # noqa: E402

OWNER = "owner@example.com"
LISTED = "bob@example.com"
OTHER = "alice@example.com"


def _run(shared_with, user_id, is_admin=False):
    vp = {"id": "vp1", "status": "JOINED", "Owner": OWNER, "SharedWith": shared_with}
    response = MagicMock()
    response.json.return_value = {"data": {"getVirtualParticipant": vp}}
    with (
        patch.object(get_virtual_participant_status.boto3, "Session") as session,
        patch("botocore.auth.SigV4Auth.add_auth"),
        patch("requests.post", return_value=response),
    ):
        session.return_value.region_name = "us-east-1"
        return get_virtual_participant_status.execute("vp1", user_id=user_id, is_admin=is_admin)


class VirtualParticipantStatusAccessTest(unittest.TestCase):
    def test_answers_each_entry_of_every_stored_shape(self):
        for shared_with in (
            f"{OTHER},{LISTED}",
            f"{OTHER}, {LISTED}",
            f"[{OTHER}, {LISTED}]",
            [OTHER, LISTED],
        ):
            with self.subTest(shared_with=shared_with):
                self.assertEqual(_run(shared_with, LISTED)["status"], "JOINED")

    def test_answers_the_owner_and_admins(self):
        self.assertEqual(_run("", OWNER)["status"], "JOINED")
        self.assertEqual(_run("", "admin@example.com", is_admin=True)["status"], "JOINED")

    def test_refuses_a_caller_who_is_not_a_whole_entry(self):
        for shared_with, caller in (
            (f"{OTHER},{LISTED}", "carol@example.com"),
            (f"{OTHER},{LISTED}", "ob@example.com"),
            ([f"{OTHER}, {LISTED}"], LISTED),
            ([f"[{LISTED}]"], LISTED),
            (None, LISTED),
        ):
            with self.subTest(shared_with=shared_with, caller=caller):
                with self.assertRaises(PermissionError):
                    _run(shared_with, caller)


if __name__ == "__main__":
    unittest.main()
