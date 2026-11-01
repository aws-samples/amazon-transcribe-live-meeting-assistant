# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the transcript knowledge-base query resolver's choice of model.

The model is read per request from the Amazon Bedrock Models section, whose
default is the BedrockModelId stack parameter, and falls back to that parameter's
environment variable (MODEL_ID) when the section does not name one.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("KB_ACCOUNT_ID", "123456789012")
os.environ.setdefault("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

HERE = Path(__file__).resolve().parent
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from lma_config import parameters  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location("query_kb_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_module()


class _Store:
    def __init__(self, values):
        self.values = values

    def get_section(self, _name):
        return SimpleNamespace(values=self.values)


@pytest.fixture()
def store(monkeypatch):
    def install(values):
        monkeypatch.setattr(parameters, "_SHARED_STORE", _Store(values))

    return install


def test_the_configured_model_is_used(store) -> None:
    store({"summaryModelId": "global.anthropic.claude-sonnet-4-6"})
    assert index.model_arn() == (
        f"arn:aws:bedrock:{index.KB_REGION}:123456789012:inference-profile/"
        "global.anthropic.claude-sonnet-4-6"
    )


def test_without_a_configured_model_the_environment_variable_is_used(store) -> None:
    store({})
    assert index.model_arn().endswith("inference-profile/us.anthropic.claude-sonnet-4-6")


def test_the_model_is_read_for_each_request(store) -> None:
    store({"summaryModelId": "us.amazon.nova-pro-v1:0"})
    first = index.model_arn()
    store({"summaryModelId": "us.anthropic.claude-sonnet-4-6"})
    assert first != index.model_arn()
