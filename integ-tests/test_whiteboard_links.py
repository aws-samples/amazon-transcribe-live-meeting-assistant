# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting page's links between guidance, the assistant chat and whiteboards, in the web UI.

One probe run (whiteboard_links_probe.py) publishes a guidance item to a
synthetic meeting and drives its page in headless Chromium as the Cognito test
user: the card's Ask the assistant and Add to whiteboard, the whiteboard that
makes, and the panel's Ask the assistant about. No model is called.

Opt-in: runs only with ``LMA_TEST_WHITEBOARD_LINKS=1``, and needs a Cognito test
user and Playwright's Chromium
(``pip install -r integ-tests/requirements-video.txt`` and
``python -m playwright install chromium``). About two minutes. Self-cleaning.
"""

from __future__ import annotations

import importlib
import json
import os

import cognito_test_user
import pytest
from assistant_whiteboards_probe import TEST_USER
from whiteboard_links_probe import GUIDANCE_TEXT, GUIDANCE_TITLE

ENABLED = os.environ.get("LMA_TEST_WHITEBOARD_LINKS", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_WHITEBOARD_LINKS=1"),
]


@pytest.fixture(scope="module")
def observed(client):
    for module in ("playwright", "pycognito"):
        try:
            importlib.import_module(module)
        except ImportError:
            pytest.fail(
                f"LMA_TEST_WHITEBOARD_LINKS is set but {module} is missing "
                "(pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt)"
            )
    if not cognito_test_user.available(client.region):
        pytest.fail(f"LMA_TEST_WHITEBOARD_LINKS is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from whiteboard_links_probe import run_probe

    result = run_probe(client.stack_name, region=client.region)
    print(json.dumps(result, indent=1, default=str))
    return result


def test_a_guidance_card_offers_the_assistant_and_the_whiteboards(observed) -> None:
    assert observed["published"] is True
    buttons = observed["ui"]["card_buttons"]
    assert "Ask the assistant" in buttons and "Add to whiteboard" in buttons, buttons


def test_ask_the_assistant_attaches_the_card_to_the_next_question(observed) -> None:
    assert observed["ui"]["ask_context_label"] == f"Guidance: {GUIDANCE_TITLE}"
    assert observed["ui"]["ask_placeholder"].startswith("Send to ask")


def test_add_to_whiteboard_makes_a_whiteboard_holding_the_card_as_the_users(observed) -> None:
    assert observed["ui"]["notice"].startswith("Added to")
    boards = observed["boards"]
    assert len(boards) == 1, boards
    assert boards[0]["CreatedBy"] == TEST_USER
    panel = next(p for p in boards[0]["Panels"] if p["Title"] == "From guidance")
    assert GUIDANCE_TITLE in panel["Content"] and GUIDANCE_TEXT in panel["Content"]
    assert panel["Source"] == "user"


def test_a_whiteboard_panel_offers_ask_the_assistant_about_it(observed) -> None:
    assert "Ask the assistant about From guidance" in observed["ui"]["ask_about_buttons"]
