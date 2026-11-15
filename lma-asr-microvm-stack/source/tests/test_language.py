# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The language named on a final row, from its text (asr_server.language)."""

from __future__ import annotations

import pytest
from asr_server import language as language_module
from asr_server.language import (
    MIN_CONFIDENCE,
    TextLanguageIdentifier,
    bundle_languages,
)

# The languages the default bundle (Parakeet TDT v3 with the Nemotron 3.5 preview)
# transcribes, as its model.env lists them.
DEFAULT_BUNDLE = "bg,cs,da,de,en,es,et,fi,fr,hr,hu,it,nl,pl,pt,ro,ru,sk,sv,uk"


def _fixed(language: str, probability: float):
    calls: list[str] = []

    def classify(text: str) -> tuple[str, float]:
        calls.append(text)
        return language, probability

    return classify, calls


# --- The bundle's list ------------------------------------------------------


def test_bundle_languages_are_read_as_base_codes() -> None:
    assert bundle_languages("en, es-US,FR,,") == frozenset({"en", "es", "fr"})


def test_bundle_languages_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_BUNDLE_LANGUAGES", "en,de")
    assert bundle_languages() == frozenset({"en", "de"})
    monkeypatch.delenv("ASR_BUNDLE_LANGUAGES")
    assert bundle_languages() == frozenset()


# --- The rules, with the classifier injected --------------------------------


def test_a_row_of_three_or_more_words_gets_the_classified_language() -> None:
    classify, _ = _fixed("es", 0.99)
    identifier = TextLanguageIdentifier(["en", "es"], classifier=classify)
    assert identifier.identify("Vamos a revisar el presupuesto.") == "es"


def test_a_row_of_fewer_than_three_words_gets_no_language_and_is_not_classified() -> None:
    classify, calls = _fixed("es", 0.99)
    identifier = TextLanguageIdentifier(["en", "es"], classifier=classify)
    assert identifier.identify("Sí, claro.") is None
    assert identifier.identify("") is None
    assert calls == []


def test_a_row_below_the_confidence_threshold_gets_no_language() -> None:
    classify, _ = _fixed("es", MIN_CONFIDENCE - 0.01)
    identifier = TextLanguageIdentifier(["en", "es"], classifier=classify)
    assert identifier.identify("one two three four") is None


def test_a_language_outside_the_bundle_is_never_reported() -> None:
    classify, _ = _fixed("ja", 1.0)
    identifier = TextLanguageIdentifier(["en", "es"], classifier=classify)
    assert identifier.identify("one two three four") is None


def test_a_bundle_with_one_language_reports_none_and_never_classifies() -> None:
    classify, calls = _fixed("en", 1.0)
    identifier = TextLanguageIdentifier(["en"], classifier=classify)
    assert not identifier.enabled
    assert not identifier.preload()
    assert identifier.identify("Thanks everyone for joining today.") is None
    assert calls == []


def test_no_published_list_means_no_language() -> None:
    identifier = TextLanguageIdentifier([])
    assert identifier.identify("Thanks everyone for joining today.") is None


def test_a_classifier_error_leaves_the_row_without_a_language() -> None:
    def boom(text: str) -> tuple[str, float]:
        raise RuntimeError("bad input")

    identifier = TextLanguageIdentifier(["en", "es"], classifier=boom)
    assert identifier.identify("one two three four") is None


def test_a_classifier_that_cannot_load_turns_identification_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[frozenset[str]] = []

    def missing(languages: frozenset[str]):
        attempts.append(languages)
        raise ModuleNotFoundError("py3langid")

    monkeypatch.setattr(language_module, "_py3langid_classifier", missing)
    identifier = TextLanguageIdentifier(["en", "es"])
    assert not identifier.preload()
    assert identifier.identify("Vamos a revisar el presupuesto.") is None
    assert identifier.identify("Vamos a revisar el presupuesto.") is None
    assert len(attempts) == 1  # loaded once, not once per row
    assert not identifier.enabled


# --- The real classifier (py3langid, restricted to the bundle) --------------


@pytest.fixture(scope="module")
def default_bundle() -> TextLanguageIdentifier:
    identifier = TextLanguageIdentifier(bundle_languages(DEFAULT_BUNDLE))
    assert identifier.preload()
    return identifier


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Vamos a revisar el presupuesto del tercer trimestre.", "es"),
        ("Nous devons valider le budget du troisième trimestre.", "fr"),
        ("Wir müssen das Budget für das dritte Quartal freigeben.", "de"),
        ("Dobbiamo approvare il budget del terzo trimestre.", "it"),
        ("Precisamos aprovar o orçamento do terceiro trimestre.", "pt"),
        ("We moeten het budget voor het derde kwartaal goedkeuren.", "nl"),
        ("Musimy zatwierdzić budżet na trzeci kwartał.", "pl"),
    ],
)
def test_the_real_classifier_names_the_bundle_language_of_a_sentence(
    default_bundle: TextLanguageIdentifier, text: str, expected: str
) -> None:
    assert default_bundle.identify(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Sorry, you're on mute.",
        "Let's ping Juan about the Kubernetes rollout.",
        "I'll send the deck to Sergio and Ana after the call.",
        "The demo for Siemens is on Friday.",
        "Hold on, let me check.",
        "The customer in Madrid wants a pilot by March.",
    ],
)
def test_english_with_foreign_names_is_never_given_another_language(
    default_bundle: TextLanguageIdentifier, text: str
) -> None:
    assert default_bundle.identify(text) in ("en", None)


def test_the_real_classifier_only_chooses_among_the_bundle_languages() -> None:
    identifier = TextLanguageIdentifier(["en", "es"])
    assert identifier.identify("Wir müssen das Budget für das dritte Quartal freigeben.") != "de"


def test_a_bundle_language_the_classifier_does_not_know_is_dropped() -> None:
    identifier = TextLanguageIdentifier(["en", "es", "zz"])
    assert identifier.preload()
    assert identifier.identify("Vamos a revisar el presupuesto del tercer trimestre.") == "es"
