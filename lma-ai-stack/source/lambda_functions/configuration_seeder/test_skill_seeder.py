#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the default skills the configuration seeder stores (skill_seeder.py).

The failure this file exists to prevent is a stack update that undoes an
Admin's choice about a default skill: re-creating one they deleted, or replacing
one they edited. Each test runs the seeder over a fake skills table, changes the
table as an Admin would, runs it again and asserts what the table holds.

Also covered: every shipped SKILL.md reads to exactly what the Skills Manager's
import would store for it, and the handler reports the outcome without failing
the deployment when the table cannot be written.

No AWS calls: the skills table is a fake.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
SKILLS_MANAGER = HERE.parent / "skills_manager"
for path in (str(HERE), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

import skill_seeder  # noqa: E402

ACCOUNT = "123456789012"
LEDGER = f"{ACCOUNT}#default-skills"


def _skill_formats():
    """The Skills Manager's parser, loaded by path: it is another function's package."""
    spec = importlib.util.spec_from_file_location(
        "_skills_manager_skill_formats", SKILLS_MANAGER / "skill_formats.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses look their module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _refused(operation: str = "PutItem") -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "refused"}}, operation
    )


class FakeSkillsTable:
    """The skills table's resource API over a dict, with the conditions the seeder uses."""

    def __init__(self) -> None:
        self.items: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.puts = 0

    def query(self, **request: Any) -> Dict[str, Any]:
        partition = request["ExpressionAttributeValues"][":account"]
        return {
            "Items": [
                copy.deepcopy(item)
                for (pk, _sk), item in sorted(self.items.items())
                if pk == partition
            ]
        }

    def put_item(self, **request: Any) -> Dict[str, Any]:
        item = copy.deepcopy(request["Item"])
        key = (item["AccountId"], item["SkillId"])
        condition = request.get("ConditionExpression")
        if condition == "attribute_not_exists(SkillId)" and key in self.items:
            raise _refused()
        if condition == "UpdatedAt = :seen":
            seen = request["ExpressionAttributeValues"][":seen"]
            if key not in self.items or self.items[key].get("UpdatedAt") != seen:
                raise _refused()
        self.items[key] = item
        self.puts += 1
        return {}

    # As an Admin would, through the Skills Manager.
    def skill(self, skill_id: str) -> Dict[str, Any]:
        return self.items[(ACCOUNT, skill_id)]

    def skills(self) -> Dict[str, Dict[str, Any]]:
        return {sk: item for (pk, sk), item in self.items.items() if pk == ACCOUNT}

    def edit(self, skill_id: str, **fields: Any) -> None:
        self.items[(ACCOUNT, skill_id)].update(
            fields, UpdatedAt="2026-12-01T00:00:00Z", UpdatedBy="admin@example.com"
        )

    def delete(self, skill_id: str) -> None:
        del self.items[(ACCOUNT, skill_id)]

    def create(self, skill_id: str, name: str, instructions: str = "Mine.") -> None:
        self.items[(ACCOUNT, skill_id)] = {
            "AccountId": ACCOUNT,
            "SkillId": skill_id,
            "Name": name,
            "DisplayName": "",
            "Description": "An Admin's own skill.",
            "Instructions": instructions,
            "Files": [],
            "Metadata": {},
            "SourceFormat": "manual",
            "UpdatedAt": "2026-11-01T00:00:00Z",
        }


def _default(name: str, instructions: str = "Do the thing.\n") -> skill_seeder.DefaultSkill:
    return skill_seeder.DefaultSkill(
        name,
        {
            "Name": name,
            "DisplayName": name.title(),
            "Description": f"The {name} skill.",
            "Instructions": instructions,
            "Files": [],
            "Metadata": {},
        },
    )


def _seed(table: FakeSkillsTable, skills: List[skill_seeder.DefaultSkill]) -> Dict[str, str]:
    return skill_seeder.DefaultSkillSeeder(table, ACCOUNT).seed(skills)


SHIPPED = [_default("alpha"), _default("beta")]


# ── the rules ───────────────────────────────────────────────────────────────


def test_a_fresh_table_gets_every_default_skill_marked_as_shipped():
    table = FakeSkillsTable()
    assert _seed(table, SHIPPED) == {"alpha": "created", "beta": "created"}
    assert sorted(table.skills()) == ["alpha", "beta"]
    alpha = table.skill("alpha")
    assert alpha["SourceFormat"] == "shipped"
    assert alpha["Instructions"] == "Do the thing.\n"
    assert alpha["CreatedBy"] == alpha["UpdatedBy"] == "cloudformation"
    # Recorded in the ledger, under a partition no reader of the skills lists.
    assert (LEDGER, "alpha") in table.items
    assert all(item["AccountId"] == ACCOUNT for item in table.skills().values())


def test_a_second_run_with_nothing_changed_writes_nothing():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    puts = table.puts
    assert _seed(table, SHIPPED) == {"alpha": "unchanged", "beta": "unchanged"}
    assert table.puts == puts


def test_an_upgrade_adds_a_default_skill_the_table_does_not_have():
    table = FakeSkillsTable()
    _seed(table, SHIPPED[:1])
    assert _seed(table, SHIPPED) == {"alpha": "unchanged", "beta": "created"}
    assert sorted(table.skills()) == ["alpha", "beta"]


def test_an_edited_default_skill_is_kept_as_the_admin_left_it():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    table.edit("alpha", Instructions="Do it my way.\n")
    newer = [_default("alpha", "Do the new thing.\n"), SHIPPED[1]]
    assert _seed(table, newer)["alpha"] == "edited"
    assert table.skill("alpha")["Instructions"] == "Do it my way.\n"
    assert table.skill("alpha")["UpdatedBy"] == "admin@example.com"


def test_a_deleted_default_skill_is_not_created_again():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    table.delete("beta")
    assert _seed(table, SHIPPED)["beta"] == "deleted"
    assert _seed(table, [SHIPPED[0], _default("beta", "A newer text.\n")])["beta"] == "deleted"
    assert "beta" not in table.skills()


def test_an_admin_skill_with_a_default_skills_name_is_left_untouched():
    table = FakeSkillsTable()
    table.create("alpha", "alpha")
    before = copy.deepcopy(table.skill("alpha"))
    assert _seed(table, SHIPPED) == {"alpha": "name-in-use", "beta": "created"}
    assert table.skill("alpha") == before
    assert (LEDGER, "alpha") not in table.items


def test_a_default_skills_name_freed_later_is_seeded_then():
    table = FakeSkillsTable()
    table.create("alpha", "alpha")
    _seed(table, SHIPPED)
    table.delete("alpha")
    assert _seed(table, SHIPPED)["alpha"] == "created"
    assert table.skill("alpha")["SourceFormat"] == "shipped"


def test_an_unedited_default_skill_is_updated_to_the_new_release_text():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    created = copy.deepcopy(table.skill("alpha"))
    newer = [_default("alpha", "Do the new thing.\n"), SHIPPED[1]]
    assert _seed(table, newer) == {"alpha": "updated", "beta": "unchanged"}
    updated = table.skill("alpha")
    assert updated["Instructions"] == "Do the new thing.\n"
    assert updated["CreatedAt"] == created["CreatedAt"]
    # And a run after that finds it unedited again.
    assert _seed(table, newer)["alpha"] == "unchanged"


def test_a_save_landing_between_the_read_and_the_replacement_wins():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    real_put = table.put_item

    def admin_saves_first(**request):
        if request.get("ConditionExpression") == "UpdatedAt = :seen":
            table.edit("alpha", Instructions="Saved meanwhile.\n")
        return real_put(**request)

    table.put_item = admin_saves_first
    assert _seed(table, [_default("alpha", "Newer.\n")])["alpha"] == "deferred"
    assert table.skill("alpha")["Instructions"] == "Saved meanwhile.\n"


def test_a_skill_stored_without_its_ledger_record_is_adopted_not_duplicated():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    del table.items[(LEDGER, "alpha")]
    assert _seed(table, SHIPPED)["alpha"] == "adopted"
    assert sorted(table.skills()) == ["alpha", "beta"]
    # Adopted, so it is now tracked: deleting it keeps it deleted.
    table.delete("alpha")
    assert _seed(table, SHIPPED)["alpha"] == "deleted"


def test_a_default_skill_replaced_by_an_import_of_the_same_text_is_left_to_the_admin():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    table.edit("alpha", SourceFormat="agent-skills")
    newer = [_default("alpha", "Do the new thing.\n"), SHIPPED[1]]
    assert _seed(table, newer)["alpha"] == "edited"
    assert table.skill("alpha")["SourceFormat"] == "agent-skills"
    assert table.skill("alpha")["Instructions"] == "Do the thing.\n"


def test_an_admin_skill_holding_the_shipped_text_is_not_adopted():
    table = FakeSkillsTable()
    table.create("alpha", "alpha", instructions="Do the thing.\n")
    table.edit("alpha", DisplayName="Alpha", Description="The alpha skill.")
    assert skill_seeder.content_digest(table.skill("alpha")) == skill_seeder.content_digest(
        SHIPPED[0].fields
    )
    assert _seed(table, SHIPPED[:1]) == {"alpha": "name-in-use"}
    assert table.skill("alpha")["SourceFormat"] == "manual"
    assert (LEDGER, "alpha") not in table.items


def test_a_replacement_whose_ledger_write_failed_is_recorded_by_the_next_run():
    table = FakeSkillsTable()
    _seed(table, SHIPPED)
    real_put = table.put_item
    failed = []

    def ledger_write_fails_once(**request):
        if request["Item"]["AccountId"] == LEDGER and not failed:
            failed.append(True)
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "PutItem")
        return real_put(**request)

    table.put_item = ledger_write_fails_once
    newer = [_default("alpha", "Do the new thing.\n")]
    assert _seed(table, newer) == {"alpha": "deferred"}
    assert table.skill("alpha")["Instructions"] == "Do the new thing.\n"
    assert _seed(table, newer) == {"alpha": "adopted"}
    # Tracked again, so the release after that still updates it.
    assert _seed(table, [_default("alpha", "Newer still.\n")]) == {"alpha": "updated"}


def test_a_default_skill_whose_id_is_taken_gets_a_numbered_id():
    table = FakeSkillsTable()
    table.create("alpha", "renamed-by-an-admin")
    assert _seed(table, SHIPPED[:1]) == {"alpha": "created"}
    assert table.skills()["alpha-2"]["Name"] == "alpha"
    assert table.skills()["alpha"]["Name"] == "renamed-by-an-admin"


@pytest.mark.parametrize(
    "taken", [set(), {"alpha"}, {"alpha", "alpha-2"}, {"alpha-2"}, {"alpha", "alpha-3"}]
)
def test_a_new_default_skill_gets_the_id_the_skills_manager_would_give_it(taken):
    assert skill_seeder._new_skill_id("alpha", set(taken)) == _skill_formats().new_skill_id(
        "alpha", set(taken)
    )


def test_no_default_skill_is_seeded_past_the_skills_limit():
    table = FakeSkillsTable()
    for number in range(skill_seeder.MAX_SKILLS):
        table.create(f"skill-{number}", f"skill-{number}")
    assert _seed(table, SHIPPED) == {"alpha": "limit-reached", "beta": "limit-reached"}


def test_the_handler_reports_the_default_skills_and_never_fails_for_them():
    import lambda_function  # pylint: disable=import-outside-toplevel

    table = FakeSkillsTable()
    properties = {"SkillsTableName": "skills", "SkillsAccountId": ACCOUNT}
    outcome = lambda_function.seed_default_skills(properties, table=table)
    assert outcome == ",".join(f"{name}=created" for name in EXPECTED_DEFAULT_SKILLS)
    assert lambda_function.seed_default_skills({}, table=table) == "not-configured"

    class Broken:
        def query(self, **_request):
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "Query")

    assert lambda_function.seed_default_skills(properties, table=Broken()) == "deferred"


# ── the shipped files ───────────────────────────────────────────────────────

EXPECTED_DEFAULT_SKILLS = [
    "catch-me-up",
    "customer-call-notes",
    "decisions-and-actions",
    "follow-up-email",
    "next-meeting-prep",
]


def test_the_release_ships_the_five_default_skills():
    assert [s.name for s in skill_seeder.shipped_default_skills()] == EXPECTED_DEFAULT_SKILLS


@pytest.mark.parametrize("name", EXPECTED_DEFAULT_SKILLS)
def test_each_default_skill_is_stored_as_the_skills_import_would_store_it(name):
    formats = _skill_formats()
    path = skill_seeder.DEFAULT_SKILLS_DIR / name / "SKILL.md"
    parsed = formats.parse_skill_file("SKILL.md", path.read_bytes())
    assert parsed.notes == [], parsed.notes
    imported = parsed.skill.to_item_fields()
    assert imported.pop("SourceFormat") == formats.SOURCE_AGENT_SKILLS
    seeded = next(s for s in skill_seeder.shipped_default_skills() if s.name == name)
    assert seeded.fields == imported
    assert skill_seeder.SOURCE_SHIPPED == formats.SOURCE_SHIPPED
