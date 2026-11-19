# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Who can_read_meeting lets in: Admins, the owner, and the people a meeting is shared with."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("LOG_LEVEL", "WARNING")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools import access_helper  # noqa: E402

can_read_meeting = access_helper.can_read_meeting

OWNER = "owner@example.com"
LISTED = "bob@example.com"
OTHER = "alice@example.com"


class TestCanReadMeeting(unittest.TestCase):
    def test_the_owner_can_read(self):
        self.assertTrue(can_read_meeting({"Owner": OWNER}, OWNER))

    def test_an_older_row_names_its_owner_as_agent_id(self):
        self.assertTrue(can_read_meeting({"AgentId": OWNER}, OWNER))

    def test_owner_and_agent_id_each_count_whatever_the_other_holds(self):
        # The rule list_meetings applies: Owner or AgentId names the caller.
        row = {"Owner": OTHER, "AgentId": OWNER}
        self.assertTrue(can_read_meeting(row, OWNER))
        self.assertTrue(can_read_meeting(row, OTHER))
        self.assertFalse(can_read_meeting(row, "third@example.com"))

    def test_the_rule_is_the_one_list_meetings_applies(self):
        from tools import list_meetings

        include = list_meetings.build_filter(OWNER, False, None, None)
        rows = [
            {"Owner": OWNER},
            {"AgentId": OWNER},
            {"Owner": OTHER, "AgentId": OWNER},
            {"Owner": OWNER, "AgentId": OTHER},
            {"Owner": OTHER},
            {"AgentId": OTHER},
            {},
        ]
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(can_read_meeting(row, OWNER), include(row))

    def test_someone_else_cannot_read(self):
        self.assertFalse(can_read_meeting({"Owner": OWNER}, OTHER))
        self.assertFalse(can_read_meeting({"AgentId": OWNER}, OTHER))

    def test_an_admin_can_read_any_meeting(self):
        self.assertTrue(can_read_meeting({"Owner": OWNER}, OTHER, is_admin=True))
        self.assertTrue(can_read_meeting({}, OTHER, is_admin=True))

    def test_an_admin_without_a_user_id_can_read(self):
        self.assertTrue(can_read_meeting({"Owner": OWNER}, None, is_admin=True))

    def test_a_caller_without_an_identity_reads_nothing(self):
        for user_id in (None, ""):
            with self.subTest(user_id=user_id):
                self.assertFalse(can_read_meeting({"Owner": OWNER}, user_id))
                self.assertFalse(can_read_meeting({}, user_id))
                self.assertFalse(can_read_meeting({"SharedWith": [""]}, user_id))

    def test_a_row_without_an_owner_is_not_open_to_everyone(self):
        self.assertFalse(can_read_meeting({}, OTHER))
        self.assertFalse(can_read_meeting({"Owner": None}, OTHER))

    def test_a_person_the_meeting_is_shared_with_can_read(self):
        shapes = {
            "list": [OTHER, LISTED],
            "tuple": (LISTED,),
            "set": {LISTED},
            "comma separated": f"{OTHER},{LISTED}",
            "comma and space": f"{OTHER}, {LISTED}",
            "brackets": f"[{OTHER}, {LISTED}]",
            "single": LISTED,
            "padded": f"  {LISTED}  ",
        }
        for name, shared in shapes.items():
            with self.subTest(shape=name):
                meeting = {"Owner": OWNER, "SharedWith": shared}
                self.assertTrue(can_read_meeting(meeting, LISTED))

    def test_a_person_the_meeting_is_not_shared_with_cannot_read(self):
        shapes = {
            "list": [OTHER],
            "string": f"[{OTHER}]",
            "empty list": [],
            "empty string": "",
            "none": None,
            "number": 7,
            "map": {"S": LISTED},
        }
        for name, shared in shapes.items():
            with self.subTest(shape=name):
                meeting = {"Owner": OWNER, "SharedWith": shared}
                self.assertFalse(can_read_meeting(meeting, LISTED))

    def test_only_a_whole_entry_counts(self):
        meeting = {"Owner": OWNER, "SharedWith": [f"x{LISTED}", f"{LISTED}.org"]}

        self.assertFalse(can_read_meeting(meeting, LISTED))
        self.assertFalse(can_read_meeting({"SharedWith": LISTED}, "bob@example"))
        self.assertFalse(can_read_meeting({"SharedWith": LISTED}, "example.com"))

    def test_an_entry_with_a_separator_in_a_list_is_not_an_entry(self):
        meeting = {"SharedWith": [f"{OTHER},{LISTED}"]}

        self.assertFalse(can_read_meeting(meeting, LISTED))
        self.assertFalse(can_read_meeting(meeting, OTHER))

    def test_entries_are_compared_exactly(self):
        self.assertFalse(can_read_meeting({"SharedWith": [LISTED.upper()]}, LISTED))

    def test_the_shared_with_reader_is_the_one_the_other_readers_carry(self):
        self.assertEqual(access_helper.shared_with_entries(f"[{OTHER}, {LISTED}]"), [OTHER, LISTED])


if __name__ == "__main__":
    unittest.main()
