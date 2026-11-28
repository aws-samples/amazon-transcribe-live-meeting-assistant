# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting page's links between guidance, the assistant chat and whiteboards, in the web UI.

Creates a synthetic meeting owned by the Cognito test user (a START and a short
conversation on the CallDataStream), publishes one guidance item to it through
the hosted MCP server's ``push_meeting_guidance`` (read-write key), and opens the
meeting's page in headless Chromium signed in as the test user. Then:

1. the guidance card in the Notifications drawer has **Ask the assistant** and
   **Add to whiteboard**;
2. **Ask the assistant** opens the assistant panel with the card shown above the
   composer (read from the chat frame);
3. **Add to whiteboard**, then **Add** in the dialog, makes a whiteboard holding
   the card's text in a panel titled "From guidance" (read back with
   ``listMeetingWhiteboards``), recorded as the test user's;
4. on the Whiteboards tab, the panel has **Ask the assistant about**.

No model is called. Every call names the one meeting, the API key is revoked,
and the meeting is removed by its CallId at the end. See
docs/meeting-whiteboards.md#adding-to-a-whiteboard.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

import cognito_test_user
from meeting_whiteboards_probe import CONVERSATION, Deployment, Meeting, Probe
from mcp_api_key_probe import ProbeError
from mcp_meeting_tools_probe import _claims
from ws_stream_probe import _mint_tokens

GUIDANCE_TITLE = "Ask who owns the launch announcement"
GUIDANCE_TEXT = "Marketing will write it, but nobody has said who exactly, or by when."
PAGE_WAIT_MS = 60000


def _ui(app_url: str, username: str, password: str, call_id: str) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    seen: dict[str, Any] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": 1500, "height": 1000}).new_page()
            page.goto(f"{app_url.rstrip('/')}/#/calls/{urllib.parse.quote(call_id, safe='')}")
            page.get_by_label(re.compile("^(Username|Email)$")).first.fill(username)
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Sign in").click()
            page.get_by_text(call_id).first.wait_for(timeout=PAGE_WAIT_MS)
            # The drawer opens by itself for a notification that arrives while the
            # page is open; one published before is behind its trigger.
            page.wait_for_timeout(8000)
            if not page.get_by_text(GUIDANCE_TITLE).first.is_visible():
                page.get_by_role("button", name=re.compile("^Notifications")).first.click()
            card = page.locator("[data-testid='meeting-notification']", has_text=GUIDANCE_TITLE).first
            card.wait_for(timeout=PAGE_WAIT_MS)
            seen["card_buttons"] = [b.inner_text().strip() for b in card.get_by_role("button").all()]

            card.get_by_role("button", name="Ask the assistant").click()
            frame = page.frame_locator("iframe.meeting-assistant-chat")
            label = frame.locator("#askContextLabel")
            label.wait_for(state="visible", timeout=PAGE_WAIT_MS)
            seen["ask_context_label"] = label.inner_text()
            seen["ask_placeholder"] = frame.locator("#messageInput").get_attribute("placeholder")
            frame.locator("#askContextRemove").click()

            card.get_by_role("button", name="Add to whiteboard").click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_role("button", name="Add", exact=True).click()
            page.get_by_text("Added to").first.wait_for(timeout=PAGE_WAIT_MS)
            seen["notice"] = page.get_by_text("Added to").first.inner_text()
            page.get_by_role("button", name="Open whiteboard").first.click()
            ask_about = page.locator("button[aria-label^='Ask the assistant about']")
            ask_about.first.wait_for(timeout=PAGE_WAIT_MS)
            seen["ask_about_buttons"] = [b.get_attribute("aria-label") for b in ask_about.all()]
        finally:
            browser.close()
    return seen


def run_probe(stack_name: str, region: str | None = None) -> dict:
    """Run the scenario and report what it observed."""
    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    deployment = Deployment(stack_name, region)
    username, password = credentials
    outputs = deployment.outputs
    pool_id = (
        outputs.get("MCPServerUserPoolId")
        or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    )
    tokens = _mint_tokens(
        deployment.session.region_name, pool_id, outputs["CognitoUserPoolClientId"], username, password
    )
    owner = _claims(tokens["id"]).get("cognito:username") or username
    probe = Probe(deployment, tokens["id"])
    meeting = Meeting(deployment, owner, assisted=False)
    observed: dict[str, Any] = {"call_id": meeting.call_id, "owner": owner}
    prefix = None
    try:
        meeting.start()
        if not meeting.wait_created():
            raise ProbeError(f"meeting {meeting.call_id} was not created")
        meeting.send(CONVERSATION[:8], 0)
        key, prefix = probe.key("read-write")
        failed, published = probe.mcp(
            key,
            "push_meeting_guidance",
            {"meeting_id": meeting.call_id, "title": GUIDANCE_TITLE, "text": GUIDANCE_TEXT},
        )
        observed["published"] = not failed
        observed["ui"] = _ui(outputs["ApplicationCloudfrontEndpoint"], username, password, meeting.call_id)
        time.sleep(2)
        observed["boards"] = [
            {
                "Title": b.get("Title"),
                "CreatedBy": b.get("CreatedBy"),
                "Panels": [
                    {"Title": p.get("Title"), "Source": p.get("Source"), "Content": p.get("Content") or ""}
                    for p in b.get("Panels") or []
                ],
            }
            for b in probe.boards(meeting)
        ]
    finally:
        if prefix:
            try:
                probe.revoke(prefix)
            except Exception:  # noqa: BLE001 - the key expires with the test user's session
                pass
        try:
            meeting.end()
        except Exception:  # noqa: BLE001 - cleanup below removes the rows either way
            pass
        meeting.cleanup()
    return observed
