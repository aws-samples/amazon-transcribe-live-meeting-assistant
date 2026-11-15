# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Which Amazon Transcribe options of the Transcription section can be used together.

The Python twin of ``transcribeOptionsProblem`` in the WebSocket transcriber's
``configuration-store.ts`` (copied to the Virtual Participant), which checks a
streaming meeting's options. ``test-fixtures/transcribe-option-combinations.json``
at the repository root holds both to the same answers, so an upload refuses the
same combinations a streaming meeting does.

An upload is transcribed by a batch job, which has two differences:

- It has no preferred language, so the preferred language is not checked.
- Batch language identification needs at least two candidate languages
  (``LanguageOptions``), so one language option is refused.

Each option is the section's setting by name (``languageCode``,
``languageOptions``, ``preferredLanguage``, ``customVocabularyName``,
``customLanguageModelName``); a missing one is empty.
"""

from typing import Mapping, Optional

#: The languageCode values that identify the language from the language options.
IDENTIFY_LANGUAGE = "identify-language"
IDENTIFY_MULTIPLE_LANGUAGES = "identify-multiple-languages"
IDENTIFY_MODES = (IDENTIFY_LANGUAGE, IDENTIFY_MULTIPLE_LANGUAGES)

#: The fewest language options batch language identification accepts.
MIN_BATCH_LANGUAGE_OPTIONS = 2


def language_options(raw: Optional[str]) -> list[str]:
    """The comma-separated language options, trimmed, empty entries dropped."""
    return [code.strip() for code in (raw or "").split(",") if code.strip()]


def _text(options: Mapping[str, object], setting: str) -> str:
    value = options.get(setting)
    return "" if value is None else str(value)


def _shared_problem(options: Mapping[str, object]) -> Optional[str]:
    """The checks streaming and uploads share, for an identify mode."""
    language_code = _text(options, "languageCode")
    if _text(options, "customVocabularyName"):
        return f"a custom vocabulary cannot be used with {language_code}"
    if _text(options, "customLanguageModelName"):
        return f"a custom language model cannot be used with {language_code}"
    languages = language_options(_text(options, "languageOptions"))
    if not languages:
        return f"{language_code} needs language options"
    seen: set[str] = set()
    for code in languages:
        language = code.split("-")[0]
        if language in seen:
            return f"the language options name more than one dialect of '{language}'"
        seen.add(language)
    return None


def streaming_options_problem(options: Mapping[str, object]) -> Optional[str]:
    """Why a streaming meeting cannot use these options, or None when it can.

    The same answer as the TypeScript ``transcribeOptionsProblem``.
    """
    if _text(options, "languageCode") not in IDENTIFY_MODES:
        return None
    problem = _shared_problem(options)
    if problem:
        return problem
    preferred = _text(options, "preferredLanguage")
    if preferred not in ("None", "") and preferred not in language_options(
        _text(options, "languageOptions")
    ):
        return f"the preferred language {preferred} is not one of the language options"
    return None


def upload_options_problem(options: Mapping[str, object]) -> Optional[str]:
    """Why an upload cannot be transcribed with these options, or None when it can."""
    language_code = _text(options, "languageCode")
    if language_code not in IDENTIFY_MODES:
        return None
    problem = _shared_problem(options)
    if problem:
        return problem
    if len(language_options(_text(options, "languageOptions"))) < MIN_BATCH_LANGUAGE_OPTIONS:
        return (
            f"{language_code} needs at least {MIN_BATCH_LANGUAGE_OPTIONS} language options"
            " for an upload"
        )
    return None
