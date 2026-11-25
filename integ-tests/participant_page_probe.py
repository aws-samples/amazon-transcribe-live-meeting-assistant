# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Open a shared meeting's page in the deployed web UI as a Participant, in Chromium.

``read_shared_meeting_page`` signs in with the given Participant account, opens
the shared meeting's page (``#/shared-meetings/<id>``), waits for the given
transcript lines, and returns what the page showed: its tabs, the buttons and
switches on it, whether the meeting assistant panel and the transcript's
Auto-scroll switch are there, its breadcrumbs, and the name of every GraphQL
operation the page sent over HTTP with any error the API answered it with.
test_participant_access.py asserts on these.

Runs headless (the page needs no display). Depends on ``playwright``
(``requirements-video.txt``) and a Chromium it has installed (``python -m
playwright install chromium``).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field

# The page's route in the web UI (SHARED_MEETINGS_PATH in the UI's routes/constants.js).
SHARED_MEETINGS_PATH = "/shared-meetings"

_OPERATION = re.compile(r"^\s*(?:query|mutation|subscription)\s+\w*\s*(?:\([^)]*\))?\s*\{\s*(?:\w+\s*:\s*)?(\w+)")


@dataclass
class ParticipantPageRun:
    tabs: list = field(default_factory=list)
    buttons: list = field(default_factory=list)
    switches: list = field(default_factory=list)
    breadcrumbs: list = field(default_factory=list)
    assistant_panel: bool = False
    lines_shown: list = field(default_factory=list)
    text: str = ""
    operations: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def operation_name(body: str | None) -> str | None:
    """The first field a GraphQL request body asks for (``getParticipantMeeting``), or None."""
    try:
        query = json.loads(body or "").get("query", "")
    except (ValueError, AttributeError):
        return None
    match = _OPERATION.match(query or "")
    return match.group(1) if match else None


def shared_meeting_url(url: str, call_id: str) -> str:
    return f"{url.rstrip('/')}/#{SHARED_MEETINGS_PATH}/{urllib.parse.quote(call_id, safe='')}"


def read_shared_meeting_page(url: str, username: str, password: str, call_id: str, *,
                             lines: list, timeout_ms: int = 60000) -> ParticipantPageRun:
    """Sign in as the Participant and read the shared meeting's page once ``lines`` are shown."""
    from playwright.sync_api import sync_playwright

    run = ParticipantPageRun()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1400, "height": 1000}).new_page()

        def on_response(response) -> None:
            request = response.request
            if request.method != "POST" or "graphql" not in request.url:
                return
            name = operation_name(request.post_data)
            if not name:
                return
            run.operations.append(name)
            try:
                errors = response.json().get("errors") or []
            except Exception:  # noqa: BLE001 — not JSON: nothing to read
                errors = []
            if errors or response.status >= 400:
                run.refused.append(f"{name}: {response.status} {errors}")

        page.on("response", on_response)
        page.goto(url)
        page.get_by_label(re.compile("^(Username|Email)$")).first.fill(username)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_load_state("networkidle")
        page.goto(shared_meeting_url(url, call_id))
        try:
            for line in lines:
                page.get_by_text(line).first.wait_for(timeout=timeout_ms)
                run.lines_shown.append(line)
        except Exception as err:  # noqa: BLE001 — reported by the test, with what the page showed
            run.errors.append(f"transcript line not shown: {err}")
        # Let the page's own reads settle before reading what it sent.
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        run.tabs = [t.strip() for t in page.get_by_role("tab").all_inner_texts()]
        run.buttons = [b.strip() for b in page.get_by_role("button").all_inner_texts() if b.strip()]
        run.switches = [
            (c.get_attribute("aria-label") or c.evaluate("e => e.labels?.[0]?.innerText || ''")).strip()
            for c in page.get_by_role("checkbox").all()
        ]
        run.breadcrumbs = [b.strip() for b in page.get_by_label("Breadcrumbs").get_by_role("link").all_inner_texts()]
        run.assistant_panel = page.locator("#meeting-assistant-panel iframe[src*='strands-chat.html']").count() > 0
        run.text = page.inner_text("body")
        browser.close()
    return run
