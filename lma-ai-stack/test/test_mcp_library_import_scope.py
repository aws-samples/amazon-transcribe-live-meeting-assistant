#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The administrators' IAM-callable mutations are not callable by the stack's own roles.

``importMCPServerLibrary`` and the meeting assistant skill writes
(``createSkill``, ``updateSkill``, ``deleteSkill``, ``importSkill`` and
``importSkillLibrary``) accept the Admin group and IAM-signed callers, so that an
operator can call them with the LMA SDK and CLI. Several roles the stacks create
are granted ``appsync:GraphQL`` on every mutation (or the whole API) for their
own work. Each such policy document carries an explicit ``Deny`` on each of those
fields, and this test holds every template to that: a new broad grant without
the Deny, or a new administrator mutation left out of an existing Deny, fails
here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = [
    REPO / "lma-ai-stack/deployment/lma-ai-stack.yaml",
    REPO / "lma-ai-stack/deployment/virtual-participant-enhancements.yaml",
    REPO / "lma-virtual-participant-stack/template.yaml",
    REPO / "lma-meetingassist-setup-stack/template.yaml",
    REPO / "lma-websocket-transcriber-stack/deployment/lma-websocket-transcriber.yaml",
]
#: The mutations every broad grant must deny.
FIELDS = (
    "importMCPServerLibrary",
    "createSkill",
    "updateSkill",
    "deleteSkill",
    "importSkill",
    "importSkillLibrary",
    # Accept IAM callers for operators (the lma CLI and SDK), as a backend role
    # that may act on any meeting.
    "rejoinVirtualParticipant",
    "reopenMeeting",
)


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _texts(value: Any) -> List[str]:
    """A Resource entry as text: a string, or the template string of a !Sub."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict) and "Fn::Sub" in value:
        sub = value["Fn::Sub"]
        return [sub if isinstance(sub, str) else sub[0]]
    return []


def _listed(value: Any) -> List[Any]:
    return value if isinstance(value, list) else [value]


def _covers_import(resource: str) -> bool:
    """Whether an Allow on ``resource`` reaches the import mutation."""
    if resource == "*" or resource.endswith("/types/Mutation/*"):
        return True
    # The whole API ("<api>/*"), as against one type ("<api>/types/Query/*").
    return resource.endswith("/*") and "/types/" not in resource


def _documents(node: Any, where: str = "") -> Iterator[Tuple[str, dict]]:
    if isinstance(node, dict):
        if isinstance(node.get("Statement"), list):
            yield where, node
        for key, value in node.items():
            yield from _documents(value, f"{where}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _documents(value, f"{where}[{index}]")


def _broad_grants() -> List[Any]:
    found = []
    for template in TEMPLATES:
        body = yaml.load(template.read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506
        for where, document in _documents(body.get("Resources", {})):
            for statement in document["Statement"]:
                if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                    continue
                if "appsync:GraphQL" not in _listed(statement.get("Action")):
                    continue
                resources = [t for r in _listed(statement.get("Resource")) for t in _texts(r)]
                if any(_covers_import(r) for r in resources):
                    found.append(
                        pytest.param(document, id=f"{template.relative_to(REPO)}:{where}")
                    )
    return found


BROAD_GRANTS = _broad_grants()


def test_the_broad_grants_are_found() -> None:
    """Guards the scan: the five roles this was written for must be among them."""
    assert len(BROAD_GRANTS) >= 5


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("document", BROAD_GRANTS)
def test_a_role_granted_every_mutation_is_denied_the_administrator_mutations(
    document: dict, field: str
) -> None:
    denied = [
        text
        for statement in document["Statement"]
        if isinstance(statement, dict) and statement.get("Effect") == "Deny"
        and "appsync:GraphQL" in _listed(statement.get("Action"))
        for resource in _listed(statement.get("Resource"))
        for text in _texts(resource)
    ]
    assert any(text.endswith(f"types/Mutation/fields/{field}") for text in denied), denied
