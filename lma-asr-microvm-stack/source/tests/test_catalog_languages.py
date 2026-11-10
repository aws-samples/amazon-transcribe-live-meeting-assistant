# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Each bundle's language list follows from its models, and always includes English.

The AsrImageSource custom resource reports the deployed bundle's languages
(``bundle_languages`` in ``lambda_functions/asr_image_source/index.py``) as the
ASR stack's ``AsrModelLanguages`` output. The WebSocket transcriber and the
Virtual Participant keep a meeting on Amazon Transcribe unless its base language
is in that list (``isMicrovmLanguage`` in their ``configuration-store.ts``), and
they treat English as served whatever the list says, which is also their rule for
a deployment that publishes no list. These tests hold the catalog to both halves
of that: a bundle's list is the languages every model writing its text
transcribes, and English is one of them for every bundle.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[2]
CATALOG = json.loads((STACK / "source" / "catalog.json").read_text(encoding="utf-8"))
MODELS = {model["id"]: model for model in CATALOG["models"]}


def _image_source():
    """The custom resource module, loaded without boto3 or its CloudFormation helper."""
    stubs = {
        "boto3": types.SimpleNamespace(client=lambda *_args, **_kwargs: None),
        "cfn_response": types.SimpleNamespace(),
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        path = STACK / "lambda_functions" / "asr_image_source" / "index.py"
        spec = importlib.util.spec_from_file_location("asr_image_source_index", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


IMAGE_SOURCE = _image_source()


def _models_of(bundle: dict) -> list[dict]:
    models = [MODELS[bundle["modelId"]]]
    preview = bundle.get("previewModelId")
    if preview and preview != "none":
        models.append(MODELS[preview])
    return models


def _languages_of(bundle: dict) -> list[str]:
    models = _models_of(bundle)
    return IMAGE_SOURCE.bundle_languages(
        {"model": models[0], "preview": models[1] if len(models) > 1 else None}
    )


@pytest.mark.parametrize("bundle", CATALOG["bundles"], ids=lambda bundle: bundle["id"])
def test_every_bundle_transcribes_english(bundle) -> None:
    assert "en" in _languages_of(bundle), (
        f"{bundle['id']}: its models do not all transcribe English, which the transcriber's "
        "and the Virtual Participant's engine choice assume of every bundle"
    )


@pytest.mark.parametrize("bundle", CATALOG["bundles"], ids=lambda bundle: bundle["id"])
def test_every_bundle_language_is_one_each_of_its_models_transcribes(bundle) -> None:
    for language in _languages_of(bundle):
        for model in _models_of(bundle):
            listed = IMAGE_SOURCE.model_languages(model)
            if listed is None:
                # A model without a list contributes nothing beyond English.
                assert language == "en", f"{bundle['id']}: {language} from {model['id']}"
            else:
                assert language in listed, (
                    f"{bundle['id']}: {language} is reported, but {model['id']} does not list it"
                )


@pytest.mark.parametrize("model", CATALOG["models"], ids=lambda model: model["id"])
def test_every_language_is_a_lower_case_base_code_or_multilingual(model) -> None:
    for code in model.get("languages") or []:
        is_base_code = code.isalpha() and code.islower() and len(code) in (2, 3)
        assert code == "multilingual" or is_base_code, (
            f"{model['id']}: {code!r} is not a base language code; the readers compare the "
            "part of a meeting's language code before the first '-'"
        )


def test_the_parakeet_v3_bundles_with_the_nemotron_preview_cover_its_european_languages() -> None:
    """The measured and documented case: the default bundle takes Spanish, German and others."""
    default = next(b for b in CATALOG["bundles"] if b["id"] == CATALOG["defaultBundleId"])
    languages = _languages_of(default)
    for code in ("en", "es", "fr", "de", "it", "pt", "nl", "pl", "sv", "ru", "uk"):
        assert code in languages
    # Japanese is the preview's but not Parakeet v3's; Greek is Parakeet v3's but not
    # one the preview transcribes out of the box.
    assert "ja" not in languages
    assert "el" not in languages


@pytest.mark.parametrize(
    "bundle_id",
    [
        # An English-only main model.
        "fastconformer-titanet-small",
        "nemotron-titanet-small",
        "parakeet-tdt-v2-titanet-small",
        # Parakeet v3 with an English-only live preview.
        "parakeet-tdt-v3-live-titanet-small",
        "parakeet-tdt-v3-live-sortformer",
        # A multilingual model the catalog lists no languages for.
        "qwen3-asr-titanet-small",
        # Decoded in the one language baked into the image.
        "cohere-transcribe-titanet-small",
    ],
)
def test_bundles_with_an_english_only_model_report_english_only(bundle_id) -> None:
    bundle = next(b for b in CATALOG["bundles"] if b["id"] == bundle_id)
    assert _languages_of(bundle) == ["en"]
