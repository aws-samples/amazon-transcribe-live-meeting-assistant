# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Every voice-enrollment function must agree on what the terms say.

The consent text states the retention period, so ``terms_text()`` -- and
therefore the SHA-256 that every enrollment is checked against -- is a function
of ``VOICE_ENROLLMENT_RETENTION_DAYS``. Three Lambdas load ``index.py``, each in
its own process with its own environment.

A function that computes a different value for one of those inputs hashes
*different words* from the function that wrote the enrollment. Nothing raises:
the mismatch is a filter, so every voiceprint is quietly judged unusable and
meetings are never seeded. That is how it shipped once, and in CloudWatch it
looks like a count of zero.

A unit test cannot catch it, because a test process has one environment. So
this reads the template and compares what each function is actually given.

It compares the **resolved value**, not the presence of the variable's name. An
earlier version asserted only that the name appeared somewhere in the block,
which a hardcoded literal in one function, or the name sitting in a comment,
both satisfied -- reproducing the original defect with the test green.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "deployment" / "lma-ai-stack.yaml"

# Variables that change what terms_text() returns, and so change its hash.
# Adding an interpolation to CONSENT_TERMS means adding its variable here.
TERMS_INPUTS = ("VOICE_ENROLLMENT_TERMS_VERSION", "VOICE_ENROLLMENT_RETENTION_DAYS")

# The module whose functions must agree. Matched on CodeUri rather than on
# handler name, so a NEW handler added to this module is covered automatically
# -- a list of handler names would have silently excluded it.
MODULE = "voice_enrollment_manager"

# How many functions that module is expected to have. Pinned exactly: adding
# one should make somebody read this file and decide whether it needs the same
# environment, rather than slipping past a >= bound.
EXPECTED_FUNCTIONS = 3


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation's short-form tags intact."""


def _tagged(loader, suffix, node):
    """Represent `!Ref Foo` as `{"Ref": "Foo"}`, as the long form would be."""
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_Loader.add_multi_constructor("!", _tagged)


def _voice_functions():
    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)
    out = {}
    for name, resource in (template.get("Resources") or {}).items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource.get("Properties") or {}
        if MODULE not in str(properties.get("CodeUri", "")):
            continue
        out[name] = ((properties.get("Environment") or {}).get("Variables") or {})
    return out


VOICE_FUNCTIONS = _voice_functions()


def test_the_voice_enrollment_functions_were_all_found():
    """A guard on the guard: a parser that matches nothing proves nothing."""
    assert sorted(VOICE_FUNCTIONS) and len(VOICE_FUNCTIONS) == EXPECTED_FUNCTIONS, (
        f"expected {EXPECTED_FUNCTIONS} functions in {MODULE}, found "
        f"{sorted(VOICE_FUNCTIONS)}. If one was added, give it the same terms "
        f"inputs as the others and update EXPECTED_FUNCTIONS."
    )


@pytest.mark.parametrize("variable", TERMS_INPUTS)
def test_every_function_resolves_a_terms_input_the_same_way(variable):
    """Not merely present -- the same value, from the same parameter."""
    given = {}
    for name, variables in VOICE_FUNCTIONS.items():
        assert variable in variables, (
            f"{name} does not receive {variable}. It loads the {MODULE} module, "
            f"so it will compute a different terms hash from the functions that "
            f"do, and silently treat every enrollment as unusable."
        )
        given[name] = variables[variable]

    distinct = {repr(value) for value in given.values()}
    assert len(distinct) == 1, (
        f"{variable} resolves differently per function: {given}. Each would hash "
        f"different consent terms, so an enrollment written by one is refused by "
        f"another."
    )


@pytest.mark.parametrize("variable", TERMS_INPUTS)
def test_a_terms_input_comes_from_a_stack_parameter_not_a_literal(variable):
    """A literal cannot stay in step with the parameter the operator sets.

    This is the shape the weaker version of this test missed: one function
    pinned to "365" while the others follow VoiceEnrollmentRetentionDays reads
    as correct and is the original defect.
    """
    for name, variables in VOICE_FUNCTIONS.items():
        value = variables.get(variable)
        assert isinstance(value, dict) and "Ref" in value, (
            f"{name} sets {variable} to {value!r} rather than referencing a "
            f"stack parameter, so it cannot follow what the operator chose."
        )


@pytest.mark.parametrize("name", sorted(VOICE_FUNCTIONS))
def test_every_function_reads_the_enrollment_table_it_was_given(name):
    """The other half of the same contract: the tables must match too."""
    assert "VOICE_ENROLLMENT_TABLE" in VOICE_FUNCTIONS[name], name
