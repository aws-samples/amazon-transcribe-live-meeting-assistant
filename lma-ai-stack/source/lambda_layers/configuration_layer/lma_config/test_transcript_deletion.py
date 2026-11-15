# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The transcript deletion setting, as its readers see it."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lma_config import parameters, transcript_deletion  # noqa: E402


class _Store:
    def __init__(self, values):
        self.values = values

    def get_section(self, name):
        assert name == "retention"
        return SimpleNamespace(values=self.values)


@pytest.mark.parametrize(
    "choice, minutes",
    [
        ("off", None),
        ("immediately", 0),
        ("after_15_minutes", 15),
        ("after_30_minutes", 30),
        ("after_60_minutes", 60),
    ],
)
def test_each_choice_is_its_delay(choice, minutes):
    store = _Store({"transcriptDeletionAfterSummary": choice})
    assert transcript_deletion.deletion_delay_minutes(store=store) == minutes


def test_nothing_saved_is_off():
    assert transcript_deletion.deletion_delay_minutes(store=_Store({})) is None


def test_a_value_that_is_not_a_choice_is_off():
    store = _Store({"transcriptDeletionAfterSummary": "after_a_while"})
    assert transcript_deletion.deletion_delay_minutes(store=store) is None


def test_no_configuration_table_is_off(monkeypatch):
    monkeypatch.setattr(parameters, "_SHARED_STORE", None)
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    assert transcript_deletion.deletion_delay_minutes() is None


def test_an_unreadable_section_is_not_taken_for_off():
    class Broken:
        def get_section(self, name):
            raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError):
        transcript_deletion.deletion_delay_minutes(store=Broken())


def test_the_schema_offers_exactly_the_choices_the_reader_knows():
    from lma_config import section_schema

    offered = section_schema("retention")["properties"]["transcriptDeletionAfterSummary"]["enum"]
    assert offered == list(transcript_deletion.DELAY_MINUTES)
