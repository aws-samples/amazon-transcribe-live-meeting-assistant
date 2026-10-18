# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Every voice-enrollment function must agree on what the terms say.

The consent text states the retention period, so ``terms_text()`` -- and
therefore the SHA-256 that every enrollment is checked against -- is a function
of ``VOICE_ENROLLMENT_RETENTION_DAYS``. Three Lambdas load ``index.py``, each in
its own process with its own environment.

A function that omits the variable falls back to the module default and hashes
*different words* from the function that wrote the enrollment. Nothing raises:
the mismatch is a filter, so every voiceprint is quietly judged unusable and
meetings are never seeded. That is how it shipped once, and it is invisible in
CloudWatch except as a count of zero.

A unit test cannot catch it, because a test process has one environment. So this
is a source-level contract test over the template: any function whose handler
lives in the voice-enrollment module must receive every variable that feeds the
terms text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "deployment" / "lma-ai-stack.yaml"
)

# Variables that change what terms_text() returns, and so change its hash.
# Adding an interpolation to CONSENT_TERMS means adding its variable here.
TERMS_INPUTS = ("VOICE_ENROLLMENT_TERMS_VERSION", "VOICE_ENROLLMENT_RETENTION_DAYS")

# The Lambdas whose code is the voice-enrollment module.
HANDLERS = ("index.handler", "seeds.handler", "retention.handler")


def function_blocks():
    """Each `Type: AWS::Serverless::Function` block, keyed by logical id."""
    text = TEMPLATE.read_text(encoding="utf-8")
    blocks = {}
    current = None
    for line in text.split("\n"):
        start = re.match(r"^  ([A-Za-z0-9]+):\s*$", line)
        if start:
            current = start.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


VOICE_FUNCTIONS = {
    name: body
    for name, body in function_blocks().items()
    if "Type: AWS::Serverless::Function" in body
    and any(f"Handler: {handler}" in body for handler in HANDLERS)
    and "voice_enrollment_manager" in body
}


def test_the_voice_enrollment_functions_were_found():
    """A guard on the guard: a parser that matches nothing proves nothing."""
    assert len(VOICE_FUNCTIONS) >= 3, sorted(VOICE_FUNCTIONS)


@pytest.mark.parametrize("name", sorted(VOICE_FUNCTIONS))
@pytest.mark.parametrize("variable", TERMS_INPUTS)
def test_every_function_agrees_on_what_the_terms_say(name, variable):
    assert f"{variable}:" in VOICE_FUNCTIONS[name], (
        f"{name} does not receive {variable}. It loads the voice-enrollment "
        f"module, so it will compute a different terms hash from the functions "
        f"that do, and silently treat every enrollment as unusable."
    )


@pytest.mark.parametrize("name", sorted(VOICE_FUNCTIONS))
def test_every_function_reads_the_enrollment_table_it_was_given(name):
    """The other half of the same contract: the tables must match too."""
    assert "VOICE_ENROLLMENT_TABLE:" in VOICE_FUNCTIONS[name], name
