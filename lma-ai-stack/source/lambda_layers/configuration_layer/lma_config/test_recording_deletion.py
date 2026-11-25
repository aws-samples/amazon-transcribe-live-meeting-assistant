# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The recording deletion setting, as its reader sees it."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lma_config import parameters, recording_deletion, transcript_deletion  # noqa: E402

DEFAULTS = (
    Path(__file__).resolve().parents[3]
    / "lambda_functions"
    / "configuration_seeder"
    / "defaults"
    / "Retention.json"
)


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
    store = _Store({"recordingDeletionAfterSummary": choice})
    assert recording_deletion.recording_deletion_delay_minutes(store=store) == minutes


def test_the_two_settings_are_read_independently():
    store = _Store(
        {"transcriptDeletionAfterSummary": "off", "recordingDeletionAfterSummary": "immediately"}
    )
    assert transcript_deletion.deletion_delay_minutes(store=store) is None
    assert recording_deletion.recording_deletion_delay_minutes(store=store) == 0
    store = _Store({"transcriptDeletionAfterSummary": "after_15_minutes"})
    assert transcript_deletion.deletion_delay_minutes(store=store) == 15
    assert recording_deletion.recording_deletion_delay_minutes(store=store) is None


def test_nothing_saved_is_off():
    assert recording_deletion.recording_deletion_delay_minutes(store=_Store({})) is None


def test_a_value_that_is_not_a_choice_is_off():
    store = _Store({"recordingDeletionAfterSummary": "after_a_while"})
    assert recording_deletion.recording_deletion_delay_minutes(store=store) is None


def test_no_configuration_table_is_off(monkeypatch):
    monkeypatch.setattr(parameters, "_SHARED_STORE", None)
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    assert recording_deletion.recording_deletion_delay_minutes() is None


def test_an_unreadable_section_is_not_taken_for_off():
    class Broken:
        def get_section(self, name):
            raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError):
        recording_deletion.recording_deletion_delay_minutes(store=Broken())


def test_the_schema_offers_exactly_the_choices_the_reader_knows():
    from lma_config import section_schema

    offered = section_schema("retention")["properties"]["recordingDeletionAfterSummary"]["enum"]
    assert offered == list(transcript_deletion.DELAY_MINUTES)


def test_the_deployment_ships_with_the_recordings_kept():
    shipped = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    assert shipped["recordingDeletionAfterSummary"] == "off"
