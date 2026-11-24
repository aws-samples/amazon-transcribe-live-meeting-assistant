# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The browser extension a deployed stack serves, driven in Chromium against it.

What it does, in order:

1. Creates a throwaway Cognito user with ``scripts/ux_test_session.py setup`` (a
   regular ``User``); :func:`delete_test_user` removes it.
2. Downloads the extension the stack serves (``ChromeExtensionDownloadUrl``), which
   carries the stack's own ``lma_config.json``. ``LMA_TEST_EXTENSION_BUILD`` names a
   local ``npm run build`` output to use instead, keeping the served configuration.
3. Loads it into Playwright's Chromium (``lma-browser-extension-stack/e2e``) and signs
   in through the extension's Login button. The button calls
   ``chrome.identity.launchWebAuthFlow``, whose window Playwright cannot drive, so the
   same Cognito hosted sign-in page is opened in a tab, filled in, and its redirect
   (``https://<extension id>.chromiumapp.org/?code=...``) is handed to the panel as
   ``launchWebAuthFlow``'s answer. Everything after that is the extension's own code:
   the code exchange at the token endpoint, the stored tokens, the signed-in panel.
4. Opens a local stand-in meeting page that plays one channel of a recorded call,
   while Chromium's fake microphone plays the other, and clicks Start Listening and
   Agree in the panel. ``--auto-accept-this-tab-capture`` answers the tab-share
   prompt of the recorder's ``getDisplayMedia({preferCurrentTab: true})``.
5. Streams for a while, clicks Stop Listening, and reads the meeting and its
   transcript segments from the stack's EventSourcing table.
6. Continues the meeting through the API: ``reopenMeeting`` as the same user, then
   a second session streamed with that ``sessionNumber`` over a WebSocket of the
   probe's own. This checks that a meeting the extension created can be continued
   by its owner; it does not drive the panel's own **Continue an existing
   meeting…** control, which the extension's unit tests cover.

The panel is driven from an ordinary tab rather than the side panel (Playwright cannot
open a side panel), and the meeting page is brought to the front before Start
Listening, because the panel captures the active tab of the last focused window.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import boto3
import ws_stream_probe

REPO_ROOT = Path(__file__).resolve().parent.parent
UX_TEST_SESSION = REPO_ROOT / "scripts" / "ux_test_session.py"
E2E_DIR = REPO_ROOT / "lma-browser-extension-stack" / "e2e"
if str(E2E_DIR) not in sys.path:
    sys.path.insert(0, str(E2E_DIR))

import extension_browser as eb  # noqa: E402

#: Two speakers, one per channel, ~5 minutes at 8 kHz (shared with ws_stream_probe).
CALL_WAV = REPO_ROOT / "utilities/load-simulator/lma_load/fixtures/stereo-demo-call.wav"
STREAM_SECONDS = float(os.environ.get("LMA_TEST_EXTENSION_STREAM_SECONDS", "45"))
CONTINUE_SECONDS = float(os.environ.get("LMA_TEST_EXTENSION_CONTINUE_SECONDS", "25"))

REOPEN_MUTATION = """
mutation ReopenMeeting($input: ReopenMeetingInput!) {
  reopenMeeting(input: $input) { CallId SessionNumber SessionOffsetSeconds ReopenedAt }
}
"""


@dataclass
class Stack:
    name: str
    region: str
    outputs: dict[str, str]
    table: str

    @property
    def graphql_url(self) -> str:
        env = self.outputs.get("LocalUITestingEnv", "")
        match = re.search(r"VITE_APPSYNC_GRAPHQL_URL=(\S+)", env)
        if not match:
            raise RuntimeError("no VITE_APPSYNC_GRAPHQL_URL in the LocalUITestingEnv output")
        return match.group(1)


def resolve_stack(name: str, region: str | None) -> Stack:
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    return Stack(
        name=name,
        region=session.region_name,
        outputs=ws_stream_probe._outputs(cfn, name),
        table=ws_stream_probe._event_sourcing_table(cfn, name),
    )


# ── the throwaway user ─────────────────────────────────────────────────────


def create_test_user(stack: Stack) -> dict[str, str]:
    """``ux_test_session.py setup``: a groupless (``User``) account with a password."""
    out = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            str(UX_TEST_SESSION),
            "setup",
            stack.name,
            "--region",
            stack.region,
            "--group",
            "User",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ux_test_session.py setup failed: {out.stderr.strip()[-500:]}")
    return json.loads(out.stdout)


def delete_test_user(stack: Stack, email: str) -> None:
    """``ux_test_session.py teardown``; raises if it exits non-zero, so a user left
    behind is reported as a teardown error instead of passing silently."""
    out = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            str(UX_TEST_SESSION),
            "teardown",
            stack.name,
            "--email",
            email,
            "--region",
            stack.region,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"ux_test_session.py teardown for {email} exited {out.returncode}: "
            f"{(out.stderr or out.stdout).strip()[-500:]}"
        )


# ── the extension ──────────────────────────────────────────────────────────


def download_served_extension(stack: Stack, dest: Path) -> Path:
    url = stack.outputs["ChromeExtensionDownloadUrl"]
    with urlopen(url, timeout=60) as response:  # noqa: S310 - the stack's own https URL
        data = response.read()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(dest)
    return dest


def chrome_accepts_version(version: str) -> bool:
    """Chrome's rule for ``manifest.json``'s ``version``: 1-4 integers of 0-65535."""
    parts = version.split(".")
    return 1 <= len(parts) <= 4 and all(
        p.isdigit() and int(p) <= 65535 and (p == "0" or not p.startswith("0")) for p in parts
    )


def stage_for_chrome(served: Path, dest: Path) -> tuple[Path, str]:
    """Stage the served extension (or a local build with its configuration).

    Returns the staged directory and the served manifest's version. A version Chrome
    refuses is re-stamped here with the SDK's publish mapping
    (``extension_browser.chrome_version``), so the rest of the run can go on;
    :func:`chrome_accepts_version` on the returned version is the test of it.
    """
    served_manifest = json.loads((served / "manifest.json").read_text())
    served_version = served_manifest["version"]
    config = json.loads((served / "lma_config.json").read_text())
    build = os.environ.get("LMA_TEST_EXTENSION_BUILD")
    source = Path(build) if build else served
    staged = eb.stage_extension(
        dest,
        build_dir=source,
        config=config,
        version=served_manifest.get("version_name") or served_version,
    )
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if not chrome_accepts_version(manifest["version"]):
        manifest["version"] = eb.chrome_version(manifest["version"])
        manifest_path.write_text(json.dumps(manifest, indent=2))
    return staged, served_version


# ── audio ──────────────────────────────────────────────────────────────────


def split_channels(src: Path, workdir: Path, seconds: float) -> tuple[Path, Path]:
    """The first ``seconds`` of a stereo WAV as two mono WAVs: (remote, local)."""
    with wave.open(str(src), "rb") as wav:
        rate, width, channels = wav.getframerate(), wav.getsampwidth(), wav.getnchannels()
        frames = wav.readframes(int(rate * seconds))
    if channels != 2:
        raise ValueError(f"{src} has {channels} channels, not 2")
    step = width * 2
    left = b"".join(frames[i : i + width] for i in range(0, len(frames), step))
    right = b"".join(frames[i + width : i + step] for i in range(0, len(frames), step))
    paths = []
    for name, pcm in (("remote.wav", left), ("local.wav", right)):
        path = workdir / name
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(width)
            out.setframerate(rate)
            out.writeframes(pcm)
        paths.append(path)
    return paths[0], paths[1]


def serve(directory: Path) -> tuple[str, Any]:
    import functools
    import http.server

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


# ── sign-in ────────────────────────────────────────────────────────────────


def hosted_sign_in(
    context: Any, config: dict[str, str], extension_id: str, email: str, password: str
) -> str:
    """Sign in on the Cognito hosted page the Login button opens; return its redirect."""
    callback = f"https://{extension_id}.chromiumapp.org/"
    query = (
        urlencode(
            {"response_type": "code", "client_id": config["clientId"], "redirect_uri": callback}
        )
        + "&scope=email+openid+profile"
    )
    redirects: list[str] = []
    page = context.new_page()
    try:
        page.route(
            f"{callback}**",
            lambda route: (
                redirects.append(route.request.url),
                route.fulfill(status=200, body="signed in"),
            ),
        )
        page.goto(f"{config['cognitoDomain']}/login?{query}")
        page.locator("input[name=username]:visible").fill(email)
        page.locator("input[name=password]:visible").fill(password)
        page.locator("input[name=signInSubmitButton]:visible").click()
        deadline = time.monotonic() + 30
        while not redirects and time.monotonic() < deadline:
            page.wait_for_timeout(250)
    finally:
        page.close()
    if not redirects or "code=" not in redirects[0]:
        raise RuntimeError(f"the hosted sign-in did not redirect with a code: {redirects}")
    return redirects[0]


# ── the run ────────────────────────────────────────────────────────────────


@dataclass
class Result:
    served_version: str = ""
    extension_id: str = ""
    signed_in: bool = False
    panel_name: str = ""
    call_id: str = ""
    start_frame: dict[str, Any] = field(default_factory=dict)
    audio_frames: int = 0
    transcribing: bool = False
    meeting: dict[str, Any] | None = field(default=None, repr=False)
    segments: list[dict[str, Any]] = field(default_factory=list, repr=False)
    page_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def stream_from_extension(
    stack: Stack,
    user: dict[str, str],
    workdir: Path,
    headless: bool = True,
    result: Result | None = None,
) -> Result:
    """Stream from the extension and fill ``result``.

    Pass ``result`` to keep what was recorded when this raises: ``call_id`` is set
    as soon as the panel sends its START frame, so the caller can still delete the
    meeting after a later step fails.
    """
    from playwright.sync_api import expect, sync_playwright

    result = Result() if result is None else result
    served = download_served_extension(stack, workdir / "served")
    staged, result.served_version = stage_for_chrome(served, workdir / "extension")
    config = json.loads((staged / "lma_config.json").read_text())

    media = workdir / "meeting"
    media.mkdir(exist_ok=True)
    (media / "meeting.html").write_text((eb.FIXTURES_DIR / "meeting.html").read_text())
    remote, local = split_channels(CALL_WAV, media, STREAM_SECONDS + 30)
    origin, server = serve(media)

    args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={local}",
        "--auto-accept-this-tab-capture",
        "--autoplay-policy=no-user-gesture-required",
    ]
    try:
        with sync_playwright() as playwright:
            context, result.extension_id = eb.launch(
                playwright, staged, workdir / "profile", extra_args=args, headless=headless
            )
            try:
                redirect = hosted_sign_in(
                    context, config, result.extension_id, user["email"], user["password"]
                )

                meeting = context.new_page()
                meeting.goto(f"{origin}/meeting.html?audio={remote.name}")
                expect(meeting.locator("iframe#permissionsIFrame")).to_have_count(1, timeout=15_000)

                panel = context.new_page()
                panel.on("pageerror", lambda error: result.page_errors.append(str(error)))
                frames: list[Any] = []

                def on_frame(payload: Any) -> None:
                    frames.append(payload)
                    if not result.call_id:
                        record_start(result, payload)

                def on_socket(ws: Any) -> None:
                    ws.on("framesent", on_frame)

                panel.on("websocket", on_socket)
                panel.goto(eb.panel_url(result.extension_id))
                expect(panel.get_by_role("button", name="Login")).to_be_visible(timeout=15_000)
                panel.evaluate(
                    "(redirect) => { chrome.identity.launchWebAuthFlow = async () => redirect; }",
                    redirect,
                )
                panel.get_by_role("button", name="Login").click()
                start = panel.get_by_role("button", name="Start Listening")
                expect(start).to_be_visible(timeout=30_000)
                result.signed_in = True
                result.panel_name = panel.get_by_placeholder("Your name").input_value()

                topic = f"ext-e2e {datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
                panel.get_by_placeholder("Meeting room topic").fill(topic)
                start.click()
                agree = panel.get_by_role("button", name="Agree")
                expect(agree).to_be_visible(timeout=10_000)
                # The panel captures the active tab of the last focused window.
                meeting.bring_to_front()
                agree.click()

                stop = panel.get_by_role("button", name="Stop Listening")
                try:
                    expect(stop).to_be_visible(timeout=30_000)
                    result.transcribing = True
                except AssertionError:
                    result.notes.append("the panel never showed Stop Listening")
                if result.transcribing:
                    meeting.wait_for_timeout(int(STREAM_SECONDS * 1000))
                    panel.bring_to_front()
                    stop.click()
                    expect(start).to_be_visible(timeout=15_000)
                    meeting.wait_for_timeout(2000)

                result.audio_frames = sum(1 for payload in frames if not isinstance(payload, str))
            finally:
                context.close()
    finally:
        server.shutdown()

    if result.call_id:
        result.meeting, result.segments = wait_for_transcript(stack, result.call_id)
    return result


def record_start(result: Result, payload: Any) -> None:
    """Keep the first START frame, and its ``callId``, from a frame the panel sent."""
    if not isinstance(payload, str) or result.start_frame:
        return
    try:
        message = json.loads(payload)
    except ValueError:
        return
    if isinstance(message, dict) and message.get("callEvent") == "START":
        result.start_frame = message
        result.call_id = str(message.get("callId") or "")


def wait_for_transcript(
    stack: Stack, call_id: str, prefix: str = "", timeout: float = 90.0
) -> tuple[dict | None, list[dict]]:
    """The meeting row and its transcript segments (ids starting ``prefix``)."""
    ddb = boto3.client("dynamodb", region_name=stack.region)
    deadline = time.monotonic() + timeout
    meeting, segments = None, []
    while time.monotonic() < deadline:
        pk = f"c#{call_id}"
        item = ddb.get_item(TableName=stack.table, Key={"PK": {"S": pk}, "SK": {"S": pk}})
        meeting = item.get("Item")
        segments = [
            s
            for s in _query(ddb, stack.table, f"trs#{call_id}")
            if s.get("SegmentId", {}).get("S", "").startswith(prefix)
        ]
        if meeting and segments:
            break
        time.sleep(5)
    return meeting, segments


def _query(ddb: Any, table: str, pk: str) -> list[dict]:
    items, kwargs = (
        [],
        {
            "TableName": table,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
        },
    )
    while True:
        page = ddb.query(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ── continuing the meeting ─────────────────────────────────────────────────


def user_tokens(stack: Stack, user: dict[str, str]) -> dict[str, str]:
    pool_id = stack.outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    return ws_stream_probe._mint_tokens(
        stack.region,
        pool_id,
        stack.outputs["CognitoUserPoolClientId"],
        user["email"],
        user["password"],
    )


def reopen_meeting(stack: Stack, id_token: str, call_id: str) -> dict[str, Any]:
    body = json.dumps({"query": REOPEN_MUTATION, "variables": {"input": {"CallId": call_id}}})
    request = Request(
        stack.graphql_url,
        data=body.encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": id_token},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - the stack's AppSync URL
        return json.loads(response.read())


def stream_session(
    stack: Stack,
    tokens: dict[str, str],
    call_id: str,
    session_number: int,
    speaker: str,
    seconds: float,
) -> dict[str, Any]:
    """A further session over the streaming API, as the Desktop Capture App sends it."""
    import websockets

    url = (
        f"{stack.outputs['LMAWebsocketEndpoint']}?authorization=Bearer%20"
        f"{tokens['access']}&id_token={tokens['id']}&refresh_token={quote(tokens['refresh'])}"
    )

    async def run() -> dict[str, Any]:
        replies: list[str] = []
        with wave.open(str(CALL_WAV), "rb") as wav:
            rate = wav.getframerate()
            chunk = int(rate * 0.2)
            wav.setpos(int(rate * 60))  # a different stretch of the call from session 1
            async with websockets.connect(url, max_size=None, open_timeout=25) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "callEvent": "START",
                            "callId": call_id,
                            "sessionNumber": session_number,
                            "localSpeakerName": speaker,
                            "remoteSpeakerName": "Continued session",
                            "samplingRate": rate,
                            "clientType": "BROWSER_EXTENSION",
                        }
                    )
                )
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    frames = wav.readframes(chunk)
                    if not frames:
                        break
                    await ws.send(frames)
                    try:
                        replies.append(await asyncio.wait_for(ws.recv(), timeout=0.1))
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                state = ws.state.name
                await ws.send(
                    json.dumps(
                        {"callEvent": "END", "callId": call_id, "sessionNumber": session_number}
                    )
                )
                await asyncio.sleep(1.0)
                return {"state": state, "close_code": ws.close_code, "replies": replies}

    try:
        return asyncio.run(run())
    except websockets.ConnectionClosed as closed:
        return {
            "state": "CLOSED",
            "close_code": closed.rcvd.code if closed.rcvd else None,
            "replies": [],
        }


def cleanup(stack: Stack, call_id: str) -> None:
    ws_stream_probe.cleanup(stack.name, call_id, stack.region)
