#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The setup custom resource finds the transcript knowledge base query function.

The function lives in the AI stack's Lambda resolvers nested stack, so the AI
stack has no resource of that logical ID; the main template passes the
function's name in instead.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.modules.setdefault("cfnresponse", types.SimpleNamespace(SUCCESS="SUCCESS", FAILED="FAILED"))

import setup_function as setup_fn  # pytest reads a module-level setup_function as a hook

ROOT = Path(__file__).resolve().parents[2]


def test_uses_the_function_name_passed_in(monkeypatch):
    def no_lookup(*_args):
        raise AssertionError("looked up in the AI stack")

    monkeypatch.setattr(setup_fn, "getStackResource", no_lookup)
    props = {"AISTACK": "arn:stack/LMA-AISTACK", "QueryKnowledgeBaseResolverFunction": "LMA-qkb-fn"}
    assert setup_fn.queryKnowledgeBaseResolverFunctionName(props) == "LMA-qkb-fn"


@pytest.mark.parametrize("props", [{"AISTACK": "arn:stack/LMA-AISTACK"},
                                   {"AISTACK": "arn:stack/LMA-AISTACK",
                                    "QueryKnowledgeBaseResolverFunction": ""}])
def test_without_a_name_looks_the_function_up_in_the_ai_stack(monkeypatch, props):
    calls = []
    monkeypatch.setattr(setup_fn, "getStackResource",
                        lambda stack, logical: calls.append((stack, logical)) or "found-fn")
    assert setup_fn.queryKnowledgeBaseResolverFunctionName(props) == "found-fn"
    assert calls == [("arn:stack/LMA-AISTACK", "QueryKnowledgeBaseResolverFunction")]


def test_the_templates_pass_the_name_from_the_resolvers_stack():
    resolvers = (ROOT / "lma-ai-stack/deployment/lma-ai-lambda-resolvers.yaml").read_text()
    ai_stack = (ROOT / "lma-ai-stack/deployment/lma-ai-stack.yaml").read_text()
    main = (ROOT / "lma-main.yaml").read_text()
    setup = (ROOT / "lma-meetingassist-setup-stack/template.yaml").read_text()
    assert "QueryKnowledgeBaseResolverFunctionName:\n    Value: !Ref QueryKnowledgeBaseResolverFunction" in resolvers
    assert "!GetAtt LambdaResolversStack.Outputs.QueryKnowledgeBaseResolverFunctionName" in ai_stack
    assert "QueryKnowledgeBaseResolverFunction: !GetAtt AISTACK.Outputs.QueryKnowledgeBaseResolverFunctionName" in main
    assert "QueryKnowledgeBaseResolverFunction: !Ref QueryKnowledgeBaseResolverFunction" in setup
