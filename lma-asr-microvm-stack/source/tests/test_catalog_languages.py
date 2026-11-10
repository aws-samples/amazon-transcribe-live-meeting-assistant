# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Every bundle the catalog ships transcribes English.

The WebSocket transcriber and the Virtual Participant keep a meeting on Amazon
Transcribe unless its language is English (``isMicrovmLanguage`` in their
``configuration-store.ts``), because the engine decodes the language its bundle
bakes in and ignores the one a session asks for. That rule is only right while
English is a language every bundle decodes: a bundle whose model cannot, or one
that bakes another language, would put English meetings on an engine that does
not transcribe them. This test holds the catalog to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CATALOG = json.loads((Path(__file__).parents[1] / "catalog.json").read_text(encoding="utf-8"))
MODELS = {model["id"]: model for model in CATALOG["models"]}


def _decodes_english(model_id: str) -> bool:
    model = MODELS[model_id]
    languages = model.get("languages") or []
    if model.get("decodeLanguage") not in (None, "", "en"):
        return False
    return "en" in languages or "multilingual" in languages


@pytest.mark.parametrize("bundle", CATALOG["bundles"], ids=lambda bundle: bundle["id"])
def test_every_bundle_decodes_english(bundle) -> None:
    assert _decodes_english(bundle["modelId"]), (
        f"{bundle['id']}: its model {bundle['modelId']} does not decode English, which the "
        "transcriber's and the Virtual Participant's engine choice assume of every bundle"
    )
    preview = bundle.get("previewModelId")
    if preview and preview != "none":
        assert _decodes_english(preview), (
            f"{bundle['id']}: its preview model {preview} does not decode English"
        )
