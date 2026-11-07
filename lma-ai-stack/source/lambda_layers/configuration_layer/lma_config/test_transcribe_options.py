# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Which Amazon Transcribe options can be used together, for streaming and for uploads.

The cases live in ``test-fixtures/transcribe-option-combinations.json``, which the
WebSocket transcriber's tests of ``transcribeOptionsProblem`` read too.
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lma_config import section_schema, transcribe_options  # noqa: E402

FIXTURE = HERE.parents[4] / "test-fixtures" / "transcribe-option-combinations.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
SETTINGS = (
    "languageCode",
    "languageOptions",
    "preferredLanguage",
    "customVocabularyName",
    "customLanguageModelName",
)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_streaming_refuses_what_the_typescript_check_refuses(case):
    assert transcribe_options.streaming_options_problem(case["options"]) == case["streaming"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_an_upload_refuses_what_the_fixture_says(case):
    assert transcribe_options.upload_options_problem(case["options"]) == case["upload"]


def test_every_case_names_every_option():
    for case in CASES:
        assert sorted(case["options"]) == sorted(SETTINGS), case["name"]


def test_a_missing_option_is_empty():
    assert transcribe_options.upload_options_problem({"languageCode": "identify-language"}) == (
        "identify-language needs language options"
    )
    assert transcribe_options.upload_options_problem({}) is None


def test_the_identify_modes_are_the_schema_s():
    offered = section_schema("transcription")["properties"]["languageCode"]["enum"]
    assert set(transcribe_options.IDENTIFY_MODES) <= set(offered)


@pytest.mark.parametrize(
    "language_code",
    [
        code
        for code in section_schema("transcription")["properties"]["languageCode"]["enum"]
        if code not in transcribe_options.IDENTIFY_MODES
    ],
)
def test_any_specific_language_is_usable_with_any_other_option(language_code):
    options = {
        "languageCode": language_code,
        "languageOptions": "",
        "preferredLanguage": "None",
        "customVocabularyName": "vocab-1",
        "customLanguageModelName": "clm-1",
    }
    assert transcribe_options.streaming_options_problem(options) is None
    assert transcribe_options.upload_options_problem(options) is None


def test_language_options_are_trimmed_and_empty_entries_dropped():
    assert transcribe_options.language_options(" en-US , ,es-US,") == ["en-US", "es-US"]
    assert transcribe_options.language_options(None) == []
