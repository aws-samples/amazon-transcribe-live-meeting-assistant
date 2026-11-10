#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Refresh the catalogue of Amazon Bedrock model ids the inference geography resolves to.

The Configuration page's **Model inference geography** setting (the Amazon Bedrock
Models section's ``inferenceGeography``) decides whether a ``global.`` model id is
run as chosen, through the inference profile of the deployment's own geography,
or as the plain in-region model id. Which of those ids exist depends on the
model and on the region the call is made from, so the resolver reads a
catalogue of them rather than guessing: for each region, the geography and
in-region ids that Amazon Bedrock lists for every model LMA offers.

This script rebuilds that catalogue from two read-only Amazon Bedrock calls per
region: ``ListInferenceProfiles`` (system-defined profiles) and
``ListFoundationModels`` filtered to on-demand throughput. A region the calling
account cannot reach (an opt-in region it has not enabled, for example) is left
out of the catalogue; the resolver treats a region it has no entry for by rule,
and says in its log that it could not check the id.

The catalogue's other keys (the profile prefixes, and which geography each
region belongs to) are hand-maintained and kept as they are.

The canonical copy is in the configuration layer; the Configuration page and
the Virtual Participant carry byte-identical copies, and ``lma-ai-stack/test/test_inference_geography.py``
asserts they match.

Usage::

    AWS_PROFILE=default python3 scripts/bedrock_inference_catalog.py
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
LMA_CONFIG = (
    REPO_ROOT / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer" / "lma_config"
)
CATALOG = LMA_CONFIG / "bedrock_inference_catalog.json"
COPIES = (
    REPO_ROOT
    / "lma-ai-stack"
    / "source"
    / "ui"
    / "src"
    / "components"
    / "configuration"
    / "bedrock-inference-catalog.json",
    REPO_ROOT
    / "lma-virtual-participant-stack"
    / "backend"
    / "src"
    / "bedrock-inference-catalog.json",
)

#: Schema settings whose choices are Amazon Bedrock model ids: (schema file, setting).
MODEL_SETTINGS = (
    ("language_models.json", "summaryModelId"),
    ("language_models.json", "meetingAssistModelId"),
    ("language_models.json", "knowledgeBaseModelId"),
    ("language_models.json", "keyframeTextModelId"),
    ("meeting_consent.json", "verifierModelId"),
)

#: Regions queried by default: the commercial regions Amazon Bedrock serves.
DEFAULT_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "ca-west-1",
    "eu-central-1",
    "eu-central-2",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-south-1",
    "ap-south-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "ap-southeast-4",
    "sa-east-1",
    "me-central-1",
    "il-central-1",
)


def offered_model_ids() -> Set[str]:
    """Every model id a model setting's schema offers."""
    ids: Set[str] = set()
    for file_name, setting in MODEL_SETTINGS:
        schema = json.loads((LMA_CONFIG / "schemas" / file_name).read_text(encoding="utf-8"))
        ids.update(schema["properties"][setting]["enum"])
    return ids


def split(model_id: str, prefixes: Iterable[str]) -> Tuple[str, str]:
    """(profile prefix or "", bare model id)."""
    head, _, rest = model_id.partition(".")
    if rest and head in set(prefixes):
        return head, rest
    return "", model_id


def list_region(region: str, bases: Set[str], prefixes: Iterable[str]) -> Dict[str, List[str]]:
    """The non-global profiles and the on-demand in-region ids of ``bases`` in ``region``."""
    import boto3  # pylint: disable=import-outside-toplevel
    from botocore.config import Config  # pylint: disable=import-outside-toplevel

    client = boto3.client(
        "bedrock",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
    )
    profiles: Set[str] = set()
    token = None
    while True:
        kwargs = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
        if token:
            kwargs["nextToken"] = token
        page = client.list_inference_profiles(**kwargs)
        for summary in page.get("inferenceProfileSummaries", []):
            prefix, base = split(summary["inferenceProfileId"], prefixes)
            if prefix and prefix != "global" and base in bases:
                profiles.add(summary["inferenceProfileId"])
        token = page.get("nextToken")
        if not token:
            break
    models = client.list_foundation_models(byInferenceType="ON_DEMAND")
    in_region = {m["modelId"] for m in models.get("modelSummaries", []) if m["modelId"] in bases}
    return {"profiles": sorted(profiles), "inRegion": sorted(in_region)}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--regions", nargs="*", default=list(DEFAULT_REGIONS))
    args = parser.parse_args(argv)

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    prefixes = list(catalog["profilePrefixes"])
    bases = sorted({split(m, prefixes)[1] for m in offered_model_ids()})

    regions: Dict[str, Dict[str, List[str]]] = {}
    for region in args.regions:
        try:
            regions[region] = list_region(region, set(bases), prefixes)
        except Exception as error:  # noqa: BLE001 - an unreachable region is skipped, not fatal
            print(f"{region}: skipped ({type(error).__name__}: {error})", file=sys.stderr)
            continue
        listed = regions[region]
        print(f"{region}: {len(listed['profiles'])} profiles, {len(listed['inRegion'])} in-region")

    catalog["listedOn"] = datetime.date.today().isoformat()
    catalog["models"] = bases
    catalog["regions"] = dict(sorted(regions.items()))
    text = json.dumps(catalog, indent=2) + "\n"
    CATALOG.write_text(text, encoding="utf-8")
    for copy in COPIES:
        copy.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
