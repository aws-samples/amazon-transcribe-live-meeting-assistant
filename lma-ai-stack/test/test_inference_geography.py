# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The Model inference geography across the stack (no AWS).

``lma_config/inference_geography.py`` decides which Amazon Bedrock id each model
setting is run as; its own tests are beside it. These hold everything around it
to it:

* **every reader resolves**: each ``read_setting`` of a model setting, in every
  function of the AI and meeting-assist stacks, is passed straight to
  ``resolve_model_id``, so no feature calls Amazon Bedrock with an id the
  setting has not seen;
* **the ports agree**: the Configuration page's and the Virtual Participant's
  copies of the catalogue are byte-identical to the layer's, and the table of
  cases their JavaScript and TypeScript ports are tested against is what the
  Python resolver answers;
* **every id it can produce has a price**: the meeting cost estimate finds an
  entry for it, its own or a named stand-in.

Regenerate the cases after changing the resolver or the catalogue with
``LMA_UPDATE_GEOGRAPHY_CASES=1 pytest lma-ai-stack/test/test_inference_geography.py``.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

REPO = Path(__file__).resolve().parents[2]
LAYER = REPO / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
AI_FUNCTIONS = REPO / "lma-ai-stack" / "source" / "lambda_functions"
MEETING_ASSIST_SRC = REPO / "lma-meetingassist-setup-stack" / "src"
UI_CONFIGURATION = REPO / "lma-ai-stack" / "source" / "ui" / "src" / "components" / "configuration"
CATALOG = LAYER / "lma_config" / "bedrock_inference_catalog.json"
UI_CATALOG = UI_CONFIGURATION / "bedrock-inference-catalog.json"
UI_CASES = UI_CONFIGURATION / "inference-geography-cases.json"
VP_CATALOG = (
    REPO / "lma-virtual-participant-stack" / "backend" / "src" / "bedrock-inference-catalog.json"
)
CALL_PRICING = AI_FUNCTIONS / "configuration_seeder" / "defaults" / "CallPricing.json"

if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from lma_config import inference_geography as geo  # noqa: E402
from lma_config import schema  # noqa: E402

MODEL_SETTINGS = (
    ("language_models", "summaryModelId"),
    ("language_models", "meetingAssistModelId"),
    ("language_models", "knowledgeBaseModelId"),
    ("language_models", "keyframeTextModelId"),
    ("meeting_consent", "verifierModelId"),
)

#: The models the Configuration page's cases cover: the shipped defaults, and one
#: of each other shape (a global Nova profile, a us. Claude profile, a plain Nova
#: id, a Claude model only some geographies offer).
CASE_MODELS = (
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "amazon.nova-lite-v1:0",
    "amazon.nova-micro-v1:0",
    "global.amazon.nova-2-lite-v1:0",
    "us.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-opus-5-5",
)

#: Regions the catalogue has no entry for, to cover the by-rule path.
UNLISTED_REGIONS = ("us-gov-west-1", "eu-south-2", "me-central-1")


def offered_models() -> List[str]:
    ids = set()
    for section, setting in MODEL_SETTINGS:
        ids.update(schema.section_schema(section)["properties"][setting]["enum"])
    return sorted(ids)


def resolution_or_none(model_id: str, geography: str, region: str) -> Optional[geo.ModelResolution]:
    try:
        return geo.resolve_for_geography(model_id, geography, region)
    except geo.ModelUnavailableInGeography:
        return None


# ── every reader resolves ───────────────────────────────────────────────────


def _model_reads_not_resolved(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    resolved_args = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name == "resolve_model_id" and node.args:
                resolved_args.add(id(node.args[0]))
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        if name != "read_setting" or len(node.args) < 2:
            continue
        setting = node.args[1]
        if isinstance(setting, ast.Constant) and str(setting.value).endswith("ModelId"):
            if id(node) not in resolved_args:
                problems.append(f"{path.relative_to(REPO)}:{node.lineno} {setting.value}")
    return problems


def _sources() -> List[Path]:
    paths = [p for p in AI_FUNCTIONS.rglob("*.py") if not p.name.startswith("test_")]
    paths += [p for p in MEETING_ASSIST_SRC.glob("*.py")]
    return [p for p in paths if "node_modules" not in p.parts and ".aws-sam" not in p.parts]


def test_every_model_setting_read_is_resolved() -> None:
    problems = [problem for path in _sources() for problem in _model_reads_not_resolved(path)]
    assert not problems, "read_setting of a model not passed to resolve_model_id: " + ", ".join(
        problems
    )


def test_the_readers_that_resolve_are_the_expected_ones() -> None:
    """A reader that stops reading its model through read_setting would escape the scan."""
    resolving = {
        path.relative_to(REPO).as_posix()
        for path in _sources()
        if "resolve_model_id(" in path.read_text(encoding="utf-8")
    }
    assert resolving >= {
        "lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/index.py",
        "lma-ai-stack/source/lambda_functions/agenda_deriver/index.py",
        "lma-ai-stack/source/lambda_functions/meeting_invitation_parser/index.py",
        "lma-ai-stack/source/lambda_functions/speaker_name_resolver/index.py",
        "lma-ai-stack/source/lambda_functions/keyframe_text_extractor/index.py",
        "lma-ai-stack/source/lambda_functions/query_knowledgebase_resolver/index.py",
        "lma-ai-stack/source/lambda_functions/mcp_analytics/tools/search_meetings.py",
        "lma-ai-stack/source/lambda_functions/consent_evidence_verifier/index.py",
        "lma-meetingassist-setup-stack/src/strands_meeting_assist_function.py",
        "lma-meetingassist-setup-stack/src/troubleshooting_agent_function.py",
    }


# ── the Configuration page agrees ───────────────────────────────────────────


@pytest.mark.parametrize("copy", [UI_CATALOG, VP_CATALOG], ids=["ui", "virtual-participant"])
def test_every_port_ships_the_same_catalogue(copy) -> None:
    assert copy.read_bytes() == CATALOG.read_bytes(), (
        "run scripts/bedrock_inference_catalog.py, or copy the layer's catalogue over this one"
    )


def expected_cases() -> List[Dict[str, object]]:
    regions = sorted(geo.catalog()["regions"]) + list(UNLISTED_REGIONS)
    cases = []
    for region in regions:
        for geography in (geo.GEOGRAPHY, geo.IN_REGION):
            for model_id in CASE_MODELS:
                resolution = resolution_or_none(model_id, geography, region)
                cases.append(
                    {
                        "region": region,
                        "geography": geography,
                        "model": model_id,
                        "expected": resolution.model_id if resolution else None,
                        "verified": resolution.verified if resolution else True,
                    }
                )
    return cases


def test_the_javascript_port_is_tested_against_the_python_answers() -> None:
    cases = expected_cases()
    if os.environ.get("LMA_UPDATE_GEOGRAPHY_CASES"):
        lines = ",\n".join(json.dumps(case) for case in cases)
        UI_CASES.write_text(f"[\n{lines}\n]\n", encoding="utf-8")
    assert set(CASE_MODELS) <= set(offered_models())
    assert json.loads(UI_CASES.read_text(encoding="utf-8")) == cases, (
        "regenerate with LMA_UPDATE_GEOGRAPHY_CASES=1"
    )


# ── every id it can produce has a price ─────────────────────────────────────


def test_every_resolvable_id_has_a_price_or_a_stand_in() -> None:
    prices = json.loads(CALL_PRICING.read_text(encoding="utf-8"))["regions"]["default"]
    unpriced = set()
    regions = sorted(geo.catalog()["regions"]) + list(UNLISTED_REGIONS)
    for region in regions:
        for geography in geo.GEOGRAPHIES:
            for model_id in offered_models():
                resolution = resolution_or_none(model_id, geography, region)
                if resolution is None:
                    continue
                service = f"bedrock/{resolution.model_id}"
                if service in prices:
                    continue
                if not any(stand_in in prices for stand_in in geo.price_stand_ins(service)):
                    unpriced.add(service)
    assert not unpriced, sorted(unpriced)


@pytest.mark.parametrize("model_id", offered_models())
def test_every_offered_id_is_priced_as_chosen(model_id) -> None:
    prices = json.loads(CALL_PRICING.read_text(encoding="utf-8"))["regions"]["default"]
    service = f"bedrock/{model_id}"
    assert service in prices or any(s in prices for s in geo.price_stand_ins(service))
