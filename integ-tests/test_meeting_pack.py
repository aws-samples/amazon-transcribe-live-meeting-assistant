# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Download › Meeting pack and Email participants, in the deployed web UI.

The test makes a synthetic meeting owned by the Cognito test user: a START on
the call data stream with a participant list (two addresses, one name in
Japanese), an END, a summary written to the meeting record, two key frames of
the shared screen with real JPEG pictures in the recordings bucket, and one
whiteboard made through ``createMeetingWhiteboard`` and filled through
``updateMeetingWhiteboard`` with a flowchart panel, a table panel and a task
list. Then, in headless Chromium signed in as the test user, on the meeting's
page:

``test_the_meeting_pack_holds_the_three_pdfs``
    Download › Meeting pack (PDF files)… opens with Summary, Slides and
    Whiteboards ticked, builds each in the browser with its size shown, and
    Download saves one .zip holding the three PDF files, the Slides PDF with a
    cover and a page per key frame and the Whiteboards PDF with the flowchart
    drawn as a picture.
``test_whiteboards_pdf_is_its_own_download``
    Download › Whiteboards (PDF) saves the Whiteboards PDF alone.
``test_email_participants_saves_an_eml_draft``
    Summary › Email participants… lists both participants and Create email
    draft (.eml) saves a message Python's ``email`` package reads without a
    defect: ``X-Unsent: 1``, both addresses in To: (the Japanese name encoded),
    the subject "<meeting> summary", the summary as text and as HTML in
    multipart/alternative inside multipart/mixed, and the three PDF files as
    base64 attachments.

Screenshots of the dialogs and the files are kept in ``LMA_TEST_MEETING_PACK_OUT``
when it is set. Everything written for the meeting (its records, whiteboard,
key frames and pictures) is removed afterwards.

Opt-in: ``LMA_TEST_MEETING_PACK=1``, with the Cognito test user
(``cognito_test_user``) and ``requirements-video.txt`` (playwright, Pillow) and
Chromium (``python -m playwright install chromium``).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import time
import urllib.parse
import uuid
import zipfile
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

import cognito_test_user
import meeting_consent_probe
import pytest
from live_meeting import read_meeting, wait_for
from mcp_api_key_probe import _graphql, _graphql_url
from ws_stream_probe import _mint_tokens

ENABLED = os.environ.get("LMA_TEST_MEETING_PACK", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(
        not ENABLED,
        reason="set LMA_TEST_MEETING_PACK=1 to build a meeting pack in the web UI",
    ),
]

MEETING_TIMEOUT_S = 90.0
UI_TIMEOUT_MS = 120_000
OUT = os.environ.get("LMA_TEST_MEETING_PACK_OUT", "")
PARTICIPANTS = [
    {"name": "Integ Ana", "email": "integ-ana@example.com"},
    {"name": "山田 太郎", "email": "integ-yamada@example.com"},
]
SUMMARY = {
    "SUMMARY": "The pack test meeting agreed the checkout plan.",
    "ACTIONS": "- **Integ Ana**: ship the checkout API",
}
FLOWCHART = 'flowchart LR\n  shopper["Shopper"] -->|HTTPS| api["Checkout API"]\n  api --> db[("Orders DB")]'
TABLE = "| Risk | Owner | Status |\n|---|---|---|\n| Review slips | Ana | Open |\n| Rate limits | Bo | Mitigated |"
TASKS = "- [x] Sandbox access\n- [ ] Security review"

CREATE = (
    "mutation C($i: CreateMeetingWhiteboardInput!) { createMeetingWhiteboard(input: $i) "
    "{ WhiteboardId Version Panels { PanelId Title Kind Content } } }"
)
UPDATE = "mutation U($i: UpdateMeetingWhiteboardInput!) { updateMeetingWhiteboard(input: $i) { WhiteboardId Version } }"


def _posix(name: str) -> str:
    return re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", name)))


def _jpeg(label: str) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1280, 720), (235, 242, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 1220, 660), outline=(9, 114, 211), width=6)
    draw.text((120, 120), label, fill=(15, 27, 42))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


def _shot(page, name: str) -> None:
    if OUT:
        Path(OUT).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(OUT) / f"{name}.png"))


def _keep(name: str, data: bytes) -> None:
    if OUT:
        Path(OUT).mkdir(parents=True, exist_ok=True)
        (Path(OUT) / name).write_bytes(data)


@pytest.fixture(scope="module")
def credentials(region):
    found = cognito_test_user.resolve(region)
    if not found:
        pytest.skip(
            f"needs the Cognito test user: {cognito_test_user.describe_sources()}"
        )
    return found


@pytest.fixture(scope="module")
def stack(stack_name, region):
    pytest.importorskip("playwright")
    pytest.importorskip("PIL")
    pytest.importorskip("pycognito")
    return meeting_consent_probe.Stack.find(stack_name, region)


@pytest.fixture(scope="module")
def meeting(stack, credentials):
    """The synthetic meeting with a summary, two key frames and a whiteboard; removed afterwards."""
    username, password = credentials
    outputs = stack.outputs
    pool_id = (
        outputs.get("MCPServerUserPoolId")
        or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    )
    tokens = _mint_tokens(
        stack.region, pool_id, outputs["CognitoUserPoolClientId"], username, password
    )
    gql_url = _graphql_url(outputs)
    call_id = f"LMA integ pack {uuid.uuid4().hex[:8]} - {datetime.now(UTC):%Y-%m-%d-%H:%M:%S.000}"
    posix = _posix(call_id)
    ddb = stack.session.client("dynamodb")
    s3 = stack.session.client("s3")
    kinesis = stack.session.client("kinesis")
    stream = outputs["CallDataStreamName"]
    frame_keys = []

    def put(record: dict) -> None:
        base = {"CallId": call_id, "AccessToken": "", "IdToken": "", "RefreshToken": ""}
        kinesis.put_record(
            StreamName=stream,
            PartitionKey=call_id,
            Data=json.dumps({**base, **record}).encode(),
        )

    created_at = datetime.now(UTC).isoformat()
    try:
        put(
            {
                "EventType": "START",
                "CustomerPhoneNumber": "Integ Test Pack",
                "SystemPhoneNumber": "LMA Integ Test",
                "AgentId": username,
                "CreatedAt": created_at,
                "Participants": PARTICIPANTS,
                "ParticipantsUpdatedBy": username,
                "ParticipantsUpdatedAt": created_at,
            }
        )
        record = wait_for(
            lambda: read_meeting(ddb, stack.event_table, call_id),
            timeout=MEETING_TIMEOUT_S,
            interval=3.0,
        )
        assert record, (
            f"the START did not create the meeting within {MEETING_TIMEOUT_S:.0f}s"
        )
        put({"EventType": "END"})
        wait_for(
            lambda: (
                (read_meeting(ddb, stack.event_table, call_id) or {}).get("Status")
                == "ENDED"
            ),
            timeout=MEETING_TIMEOUT_S,
            interval=3.0,
        )
        key = {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}
        ddb.update_item(
            TableName=stack.event_table,
            Key=key,
            UpdateExpression="SET CallSummaryText = :s",
            ExpressionAttributeValues={":s": {"S": json.dumps(SUMMARY)}},
        )
        expires = str(int(time.time()) + 86400)
        for number in (1, 2):
            frame_id = f"00000{number}0000-integpack"
            object_key = f"lma-video-keyframes/{posix}/{frame_id}.jpg"
            s3.put_object(
                Bucket=stack.recordings_bucket,
                Key=object_key,
                Body=_jpeg(f"Slide {number}"),
                ContentType="image/jpeg",
            )
            frame_keys.append(object_key)
            # The bucket's regional host, as the frame writer names it (amazonaws.com.cn in China).
            suffix = (
                "amazonaws.com.cn"
                if stack.region.startswith("cn-")
                else "amazonaws.com"
            )
            url = f"https://{stack.recordings_bucket}.s3.{stack.region}.{suffix}/{object_key}"
            ddb.put_item(
                TableName=stack.event_table,
                Item={
                    "PK": {"S": f"kf#{call_id}"},
                    "SK": {"S": f"f#{frame_id}"},
                    "CallId": {"S": call_id},
                    "FrameId": {"S": frame_id},
                    "Timestamp": {"N": str(number * 10)},
                    "ImageUrl": {"S": url},
                    "ThumbnailUrl": {"S": url},
                    "Width": {"N": "1280"},
                    "Height": {"N": "720"},
                    "Owner": {"S": username},
                    "ExpiresAfter": {"N": expires},
                },
            )
        board = _graphql(
            gql_url,
            tokens["id"],
            CREATE,
            {
                "i": {
                    "CallId": call_id,
                    "Title": "Pack test board",
                    "Template": "process",
                }
            },
        )["createMeetingWhiteboard"]
        panels = [
            {
                "PanelId": p["PanelId"],
                "Title": p["Title"],
                "Kind": p["Kind"],
                "Content": p.get("Content") or "",
            }
            for p in board["Panels"]
        ]
        for panel in panels:
            panel["Content"] = (
                FLOWCHART if panel["Kind"] == "mermaid" else f"{TABLE}\n\n{TASKS}"
            )
        _graphql(
            gql_url,
            tokens["id"],
            UPDATE,
            {
                "i": {
                    "CallId": call_id,
                    "WhiteboardId": board["WhiteboardId"],
                    "BaseVersion": board["Version"],
                    "Panels": panels,
                }
            },
        )
        yield {"call_id": call_id, "username": username, "password": password}
    finally:
        for prefix in ("c#", "trs#", "kf#", "wb#", "nt#", "mtr#"):
            with contextlib.suppress(Exception):
                items = ddb.query(
                    TableName=stack.event_table,
                    KeyConditionExpression="PK = :pk",
                    ExpressionAttributeValues={":pk": {"S": f"{prefix}{call_id}"}},
                ).get("Items", [])
                for item in items:
                    ddb.delete_item(
                        TableName=stack.event_table,
                        Key={"PK": item["PK"], "SK": item["SK"]},
                    )
        with contextlib.suppress(Exception):
            day = created_at[:10]
            shard = f"{int(created_at[11:13]) // 4:02d}"
            listed = ddb.query(
                TableName=stack.event_table,
                KeyConditionExpression="PK = :pk",
                FilterExpression="CallId = :c",
                ExpressionAttributeValues={
                    ":pk": {"S": f"cls#{day}#s#{shard}"},
                    ":c": {"S": call_id},
                },
            ).get("Items", [])
            for item in listed:
                ddb.delete_item(
                    TableName=stack.event_table,
                    Key={"PK": item["PK"], "SK": item["SK"]},
                )
        for object_key in frame_keys:
            with contextlib.suppress(Exception):
                s3.delete_object(Bucket=stack.recordings_bucket, Key=object_key)


@pytest.fixture(scope="module")
def page(stack, meeting):
    """The meeting's page, signed in as its owner, in headless Chromium."""
    from playwright.sync_api import sync_playwright

    url = stack.outputs["ApplicationCloudfrontEndpoint"].rstrip("/")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000}, accept_downloads=True
        )
        tab = context.new_page()
        tab.goto(url)
        tab.get_by_label(re.compile("^(Username|Email)$")).first.fill(
            meeting["username"]
        )
        tab.get_by_label("Password", exact=True).fill(meeting["password"])
        tab.get_by_role("button", name="Sign in").click()
        tab.wait_for_load_state("networkidle")
        tab.goto(f"{url}/#/calls/{urllib.parse.quote(meeting['call_id'], safe='')}")
        tab.get_by_role("button", name="Download").wait_for(timeout=UI_TIMEOUT_MS)
        # The whiteboard and the key frames are read once the page opens.
        tab.wait_for_load_state("networkidle")
        tab.wait_for_timeout(3000)
        yield tab
        browser.close()


def _open(page, *path: str) -> None:
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Download").click()
    for name in path:
        page.get_by_role("menuitem", name=name).first.click()


def _dialog(page):
    return page.get_by_role("dialog")


def _wait_ready(page) -> None:
    page.get_by_test_id("pack-total").filter(has_text=re.compile(r"^Total: ")).wait_for(
        timeout=UI_TIMEOUT_MS
    )


def _pdf_pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


def test_the_meeting_pack_holds_the_three_pdfs(page):
    _open(page, "Meeting pack (PDF files)…")
    dialog = _dialog(page)
    for part in ("summary", "slides", "whiteboards"):
        assert (
            dialog.get_by_test_id(f"pack-part-{part}")
            .get_by_role("checkbox")
            .is_checked()
        ), part
    _wait_ready(page)
    for part in ("summary", "slides", "whiteboards"):
        text = dialog.get_by_test_id(f"pack-part-{part}").inner_text()
        assert re.search(r"\d+(\.\d)? (B|KB|MB)", text), (
            f"{part} shows no size: {text!r}"
        )
    _shot(page, "meeting-pack-dialog")
    with page.expect_download(timeout=UI_TIMEOUT_MS) as download:
        dialog.get_by_role("button", name="Download").click()
    data = Path(download.value.path()).read_bytes()
    _keep(download.value.suggested_filename, data)
    assert download.value.suggested_filename.startswith("Meeting-pack-LMA integ pack")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        names = sorted(archive.namelist())
        assert [name.split("-")[0] for name in names] == [
            "Slides",
            "Summary",
            "Whiteboards",
        ]
        files = {name.split("-")[0]: archive.read(name) for name in names}
    for kind, body in files.items():
        assert body.startswith(b"%PDF-"), kind
    # The slides: a cover and a page per key frame, each with its picture.
    assert _pdf_pages(files["Slides"]) == 3
    assert len(re.findall(rb"/Subtype\s*/Image", files["Slides"])) >= 2
    # The whiteboards: a cover and the board, with the flowchart drawn as a picture.
    assert _pdf_pages(files["Whiteboards"]) >= 2
    assert re.search(rb"/Subtype\s*/Image", files["Whiteboards"]), (
        "the flowchart was not drawn"
    )
    dialog.get_by_role("button", name="Close").click()


def test_whiteboards_pdf_is_its_own_download(page):
    with page.expect_download(timeout=UI_TIMEOUT_MS) as download:
        _open(page, "Whiteboards (PDF)")
    assert download.value.suggested_filename.startswith("Whiteboards-LMA integ pack")
    assert Path(download.value.path()).read_bytes().startswith(b"%PDF-")


def test_email_participants_saves_an_eml_draft(page, meeting):
    _open(page, "Summary", "Summary: Email participants…")
    dialog = _dialog(page)
    assert dialog.get_by_text("Integ Ana").is_visible()
    assert dialog.get_by_text("山田 太郎").is_visible()
    _wait_ready(page)
    _shot(page, "email-participants-dialog")
    with page.expect_download(timeout=UI_TIMEOUT_MS) as download:
        dialog.get_by_role("button", name="Create email draft (.eml)").click()
    raw = Path(download.value.path()).read_bytes()
    _keep(download.value.suggested_filename, raw)
    assert download.value.suggested_filename.endswith(".eml")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert not message.defects
    assert all(not part.defects for part in message.walk())
    assert message["X-Unsent"] == "1"
    assert [a.addr_spec for a in message["To"].addresses] == [
        p["email"] for p in PARTICIPANTS
    ]
    assert [a.display_name for a in message["To"].addresses] == [
        p["name"] for p in PARTICIPANTS
    ]
    assert (
        message["Subject"].endswith(" summary")
        and "LMA integ pack" in message["Subject"]
    )
    assert message.get_content_type() == "multipart/mixed"
    alternative = next(
        part
        for part in message.iter_parts()
        if part.get_content_type() == "multipart/alternative"
    )
    assert [part.get_content_type() for part in alternative.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert "agreed the checkout plan" in message.get_body(("plain",)).get_content()
    assert "agreed the checkout plan" in message.get_body(("html",)).get_content()
    attachments = list(message.iter_attachments())
    assert sorted(a.get_filename().split("-")[0] for a in attachments) == [
        "Slides",
        "Summary",
        "Whiteboards",
    ]
    for attachment in attachments:
        assert attachment["Content-Transfer-Encoding"] == "base64"
        assert attachment.get_content().startswith(b"%PDF-")
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")
    dialog.get_by_role("button", name="Close").click()
