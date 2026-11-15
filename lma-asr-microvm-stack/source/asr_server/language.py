# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The spoken language of a final row, identified from the row's text.

None of the catalog's speech models gives this server a language for a row: the
offline models write no language at all, and the one streaming model that tags its
text (Nemotron 3.5, ``<es-ES>`` after a sentence) does so only once it has heard the
next sentence begin or a long pause, which is after the row's final has been sent.
So the final text, which in a multilingual bundle is already in the spoken
language, is classified with py3langid (BSD-3-Clause), an n-gram classifier of about
4.5 MB that runs in well under a millisecond per row.

The classifier only chooses among the deployed bundle's languages
(``$ASR_BUNDLE_LANGUAGES``, the list the ASR stack publishes as AsrModelLanguages),
and names none for

* a bundle with fewer than two languages, where the answer carries no information;
* a row of fewer than :data:`MIN_WORDS` words, where a text classifier guesses; and
* a row it scores below :data:`MIN_CONFIDENCE`.

A row with no language is reported without one, exactly as before this existed.
Only the base language (``es``) is reported: text does not say which region's
Spanish it is, so a client that wants a full locale chooses the region itself.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable

__all__ = [
    "BUNDLE_LANGUAGES_ENV",
    "MIN_CONFIDENCE",
    "MIN_WORDS",
    "Classifier",
    "TextLanguageIdentifier",
    "bundle_languages",
    "default_language_identifier",
]

_LOG = logging.getLogger(__name__)

#: model.env key listing the deployed bundle's base languages, e.g. ``en,es,fr``.
BUNDLE_LANGUAGES_ENV = "ASR_BUNDLE_LANGUAGES"

#: Rows shorter than this are not classified. On the measured set, every
#: misidentification by the classifier was on a row of one or two words.
MIN_WORDS = 3

#: Lowest normalised probability at which a row is given a language.
MIN_CONFIDENCE = 0.8

#: ``(text) -> (base language, probability)``.
Classifier = Callable[[str], tuple[str, float]]


def bundle_languages(value: str | None = None) -> frozenset[str]:
    """The deployed bundle's base languages, from ``$ASR_BUNDLE_LANGUAGES`` by default."""
    raw = os.environ.get(BUNDLE_LANGUAGES_ENV, "") if value is None else value
    return frozenset(
        code.strip().lower().split("-")[0] for code in raw.split(",") if code.strip()
    )


def _py3langid_classifier(languages: frozenset[str]) -> tuple[Classifier, frozenset[str]]:
    """A py3langid classifier restricted to ``languages`` it knows, and that subset."""
    from py3langid.langid import (  # type: ignore[import-untyped]
        MODEL_FILE,
        LanguageIdentifier,
    )

    identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    known = frozenset(languages) & frozenset(identifier.nb_classes)
    if len(known) >= 2:
        identifier.set_languages(sorted(known))

    def classify(text: str) -> tuple[str, float]:
        language, probability = identifier.classify(text)
        return str(language), float(probability)

    return classify, known


class TextLanguageIdentifier:
    """Names the language of a final row's text, or ``None`` (see the module docstring).

    Shared by every session. The classifier is built on first use, or by
    :meth:`preload` at server start so it is resident in the MicroVM snapshot. If it
    cannot be built (the library is missing), every row is reported without a
    language and the failure is logged once.
    """

    def __init__(
        self,
        languages: Iterable[str],
        *,
        classifier: Classifier | None = None,
        min_words: int = MIN_WORDS,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        self._languages = frozenset(code.strip().lower() for code in languages if code.strip())
        self._classifier = classifier
        self._min_words = min_words
        self._min_confidence = min_confidence
        self._lock = threading.Lock()
        self._failed = False

    @property
    def enabled(self) -> bool:
        """Whether rows can be given a language at all (two or more bundle languages)."""
        return len(self._languages) >= 2 and not self._failed

    def preload(self) -> bool:
        """Build the classifier now; whether rows can be given a language."""
        return self._get() is not None

    def __call__(self, text: str) -> str | None:
        return self.identify(text)

    def identify(self, text: str) -> str | None:
        """The base language of ``text``, or ``None`` when it cannot be said reliably."""
        if len(text.split()) < self._min_words:
            return None
        classify = self._get()
        if classify is None:
            return None
        try:
            language, probability = classify(text)
        except Exception:  # noqa: BLE001 - the language is optional detail
            _LOG.warning("language identification failed for one row", exc_info=True)
            return None
        if language not in self._languages or probability < self._min_confidence:
            return None
        return language

    def _get(self) -> Classifier | None:
        if not self.enabled:
            return None
        if self._classifier is not None:
            return self._classifier
        with self._lock:
            if self._classifier is None and not self._failed:
                try:
                    self._classifier, known = _py3langid_classifier(self._languages)
                except Exception:  # noqa: BLE001 - optional: rows keep no language
                    self._failed = True
                    _LOG.warning(
                        "language identification unavailable; rows carry no language",
                        exc_info=True,
                    )
                    return None
                unknown = sorted(self._languages - known)
                if unknown:
                    _LOG.warning("language identification cannot name: %s", ",".join(unknown))
                self._languages = known
                if len(known) < 2:
                    return None
        return self._classifier


_DEFAULT: TextLanguageIdentifier | None = None
_DEFAULT_LOCK = threading.Lock()


def default_language_identifier() -> TextLanguageIdentifier:
    """The process-wide identifier for the deployed bundle's languages."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = TextLanguageIdentifier(bundle_languages())
        return _DEFAULT
