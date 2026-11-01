# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""End-to-end probe of the desktop capture app's video lane, with a synthetic slideshow.

What it does, in order:

1. Renders five slides with large, distinct text (Pillow) and narrates each one
   with Amazon Polly. Every slide is held for an even number of seconds, at
   least eight, so each slide change falls on an H.264 key frame.
2. Encodes the slides the way the desktop capture app does
   (``VideoCapture.swift`` / ``VideoCapture.cs``): H.264 at 5 frames a second,
   about 1.5 Mbit/s, a key frame every two seconds, as a fragmented MP4 (an init
   segment, then one ``moof``/``mdat`` fragment per two-second group of pictures).
3. Opens a Stream Audio session over the transcriber WebSocket as the Cognito
   test user and streams the narration in real time, exactly as
   ``ws_stream_probe.py`` does. One second in, it opens the second, video-only
   socket the app uses (``VideoSocket.swift`` / ``VideoSocket.cs``): the same
   endpoint and token, a ``START_VIDEO`` text frame carrying the call id and
   ``videoTimeOffsetMs``, each fMP4 fragment as a binary frame at the moment the
   app's encoder would have finished it, then ``END_VIDEO``.
4. Ends the audio session and reads back what was stored: the meeting row,
   transcript segments, the key frame items (``kf#<CallId>``) and both images of
   every frame in the recordings bucket.

The last slide's text is deliberately never spoken, so an assistant answer that
quotes it can only have come from the stored picture, not from the transcript.

Nothing is deleted afterwards. The meeting, its transcript and its key frames
carry the deployment's retention and expire on their own, and a meeting left in
place can be opened on the meeting page to look at the Shared screen strip.

Depends on ``pycognito`` and ``websockets`` (``requirements.txt``) and on
``pillow`` and an ``ffmpeg`` with libx264 (``requirements-video.txt`` installs
``imageio-ffmpeg``, which bundles one; ``LMA_TEST_FFMPEG`` or an ``ffmpeg`` on
PATH is used first).

Standalone:
    LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user AWS_PROFILE=default \\
      python integ-tests/desktop_video_probe.py lma-integtest1 --region us-west-2

    # Only build the media into a folder, without touching AWS except Polly:
    python integ-tests/desktop_video_probe.py --media-only /tmp/slides
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import wave
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import boto3
import cognito_test_user
from ws_stream_probe import (
    _event_sourcing_table,
    _meeting_exists,
    _mint_tokens,
    _outputs,
)

SAMPLE_RATE = 16000
FPS = 5
KEYFRAME_SECONDS = 2
BITRATE = "1500k"
WIDTH, HEIGHT = 1920, 1080
MIN_SLIDE_SECONDS = 8
# Silence before the first slide, which is also the offset the video socket
# reports: the first video frame is this far into the audio.
LEAD_SECONDS = 1.0
# Silence after the last slide, so the transcriber's last audio block is flushed.
TAIL_SECONDS = 4.0
POLLY_VOICE = "Matthew"


@dataclass(frozen=True)
class Slide:
    title: str
    body: str
    narration: str
    # Band colour. Consecutive slides alternate dark and light bands, so they
    # differ in far more than the detector's 4% of its grayscale grid even if
    # the text alone would not.
    band: tuple[int, int, int]


SLIDES: tuple[Slide, ...] = (
    Slide(
        "Quarterly Business Review",
        "Third quarter results",
        "Welcome everyone. This is our quarterly business review for the third quarter.",
        (20, 30, 90),
    ),
    Slide(
        "Q3 revenue grew 12%",
        "Driven by new customers",
        "Revenue grew twelve percent compared with last quarter, mostly from new customers.",
        (230, 180, 40),
    ),
    Slide(
        "Customer churn fell to 3%",
        "Lowest in two years",
        "Customer churn came down as well. It is the best result we have had in two years.",
        (10, 80, 40),
    ),
    Slide(
        "Next steps: hire two engineers",
        "Start in November",
        "For next steps, we plan to hire two more engineers, starting in November.",
        (120, 190, 230),
    ),
    # Never spoken: the assistant check asserts the answer quotes this slide.
    Slide(
        "Launch codename: BLUE HERON",
        "Ship date: March 14",
        "And finally, the last slide has the launch details. Please read them on screen, "
        "I will not say them out loud.",
        (150, 20, 30),
    ),
)
# Words from the last slide that appear in no narration.
LAST_SLIDE_MARKERS = ("heron",)


@dataclass
class Media:
    workdir: Path
    slides: tuple[Slide, ...]
    durations: list[int]                 # seconds each slide is held
    starts: list[float]                  # slide start, seconds into the VIDEO
    pcm: bytes                           # 16 kHz, 16-bit, stereo interleaved
    mp4_path: Path
    fragments: list[bytes] = field(default_factory=list)  # [init, frag0, frag1, ...]

    @property
    def audio_seconds(self) -> float:
        return len(self.pcm) / (SAMPLE_RATE * 2 * 2)

    def expected_timestamps(self) -> list[float]:
        """Where each slide should land on the transcript's clock."""
        return [LEAD_SECONDS + s for s in self.starts]


# ── media ──────────────────────────────────────────────────────────────────

def find_ffmpeg() -> str:
    """An ffmpeg with libx264: $LMA_TEST_FFMPEG, then PATH, then imageio-ffmpeg's."""
    explicit = os.environ.get("LMA_TEST_FFMPEG")
    if explicit:
        return explicit
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg
    except ImportError as err:
        raise FileNotFoundError(
            "no ffmpeg: set LMA_TEST_FFMPEG, put ffmpeg on PATH, or "
            "pip install -r integ-tests/requirements-video.txt"
        ) from err
    return imageio_ffmpeg.get_ffmpeg_exe()


def render_slide(slide: Slide, index: int, total: int, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (WIDTH, HEIGHT), (250, 250, 247))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, WIDTH, 210], fill=slide.band)
    draw.rectangle([0, HEIGHT - 70, WIDTH, HEIGHT], fill=slide.band)
    title = ImageFont.load_default(size=118)
    body = ImageFont.load_default(size=84)
    small = ImageFont.load_default(size=40)
    draw.text((WIDTH / 2, 470), slide.title, font=title, fill=(20, 20, 20), anchor="mm")
    draw.text((WIDTH / 2, 680), slide.body, font=body, fill=(70, 70, 70), anchor="mm")
    draw.text((WIDTH - 60, HEIGHT - 35), f"{index + 1} / {total}", font=small,
              fill=(255, 255, 255), anchor="rm")
    img.save(path)


def synthesize(polly, text: str) -> bytes:
    """Mono 16 kHz 16-bit PCM of one narration."""
    resp = polly.synthesize_speech(
        Text=text, VoiceId=POLLY_VOICE, Engine="neural",
        OutputFormat="pcm", SampleRate=str(SAMPLE_RATE),
    )
    return resp["AudioStream"].read()


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * round(seconds * SAMPLE_RATE)


def build_audio(narrations: list[bytes], durations: list[int]) -> bytes:
    """Stereo PCM: narration on the left (meeting) channel, silence on the mic."""
    mono = bytearray(_silence(LEAD_SECONDS))
    for pcm, seconds in zip(narrations, durations):
        # Half a second in, so each narration starts after its slide is up.
        clip = _silence(0.5) + pcm
        target = seconds * SAMPLE_RATE * 2
        mono += clip[:target] + _silence((target - min(len(clip), target)) / (SAMPLE_RATE * 2))
    mono += _silence(TAIL_SECONDS)
    stereo = bytearray(len(mono) * 2)
    stereo[0::4] = mono[0::2]
    stereo[1::4] = mono[1::2]
    return bytes(stereo)


def encode_video(ffmpeg: str, pngs: list[Path], durations: list[int], out: Path) -> None:
    """H.264 fMP4 as the desktop app sends it: 5 fps, key frame every 2 s, ~1.5 Mbit/s."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for png, seconds in zip(pngs, durations):
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", str(seconds), "-i", str(png)]
    concat = "".join(f"[{i}:v]" for i in range(len(pngs)))
    cmd += [
        "-filter_complex", f"{concat}concat=n={len(pngs)}:v=1:a=0,format=yuv420p[v]",
        "-map", "[v]", "-r", str(FPS),
        "-c:v", "libx264", "-profile:v", "main", "-preset", "veryfast",
        "-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", "3000k",
        "-g", str(FPS * KEYFRAME_SECONDS), "-keyint_min", str(FPS * KEYFRAME_SECONDS),
        "-sc_threshold", "0",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", str(out),
    ]
    subprocess.run(cmd, check=True)


def split_fmp4(data: bytes) -> list[bytes]:
    """[init segment, fragment, fragment, ...] by top-level box.

    The init segment is everything before the first ``moof``; each fragment is a
    ``moof`` and the boxes after it up to the next ``moof`` (its ``mdat``).
    """
    boxes: list[tuple[str, bytes]] = []
    pos = 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8].decode("latin-1")
        header = 8
        if size == 1:
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = len(data) - pos
        if size < header:
            raise ValueError(f"malformed MP4 box {kind!r} at {pos}")
        boxes.append((kind, data[pos:pos + size]))
        pos += size
    parts: list[bytearray] = [bytearray()]
    for kind, box in boxes:
        if kind == "moof":
            parts.append(bytearray())
        if kind == "mfra":  # random-access index at the end; the app sends none
            continue
        parts[-1] += box
    if len(parts) < 2:
        raise ValueError("not a fragmented MP4 (no moof boxes)")
    return [bytes(p) for p in parts]


def probe_keyframes(ffmpeg: str, mp4: Path) -> list[float]:
    """Presentation times of the H.264 key frames, decoded the way the transcriber does."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-skip_frame", "nokey", "-i", str(mp4),
         "-vf", "showinfo", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    times = []
    for line in proc.stderr.splitlines():
        if "pts_time:" in line and "iskey:1" in line:
            times.append(float(line.split("pts_time:")[1].split()[0]))
    return times


def build_media(region: str | None, workdir: Path | None = None) -> Media:
    work = workdir or Path(tempfile.mkdtemp(prefix="lma-desktop-video-"))
    work.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    polly = boto3.Session(region_name=region).client("polly")
    narrations = [synthesize(polly, s.narration) for s in SLIDES]
    durations = []
    for pcm in narrations:
        spoken = 0.5 + len(pcm) / (SAMPLE_RATE * 2) + 1.0
        durations.append(max(MIN_SLIDE_SECONDS, KEYFRAME_SECONDS * math.ceil(spoken / KEYFRAME_SECONDS)))
    starts = [float(sum(durations[:i])) for i in range(len(durations))]
    pngs = []
    for i, slide in enumerate(SLIDES):
        png = work / f"slide{i + 1}.png"
        render_slide(slide, i, len(SLIDES), png)
        pngs.append(png)
    mp4 = work / "slides.mp4"
    encode_video(ffmpeg, pngs, durations, mp4)
    media = Media(work, SLIDES, durations, starts, build_audio(narrations, durations), mp4)
    media.fragments = split_fmp4(mp4.read_bytes())
    with wave.open(str(work / "narration.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(media.pcm)
    return media


# ── streaming ──────────────────────────────────────────────────────────────

def _ws_url(ws_ep: str, tokens: dict) -> str:
    return (
        f"{ws_ep}?authorization=Bearer%20{tokens['access']}"
        f"&id_token={tokens['id']}&refresh_token={tokens['refresh']}"
    )


async def _stream(ws_ep: str, tokens: dict, call_id: str, agent_id: str, media: Media) -> dict:
    """Audio socket + video socket, both paced against one clock. Returns socket states."""
    import websockets

    url = _ws_url(ws_ep, tokens)
    observed: dict = {"audio_state": None, "video_state": None, "video_close": None,
                      "fragments_sent": 0, "video_bytes": 0}
    chunk_ms = 200
    chunk_bytes = SAMPLE_RATE * chunk_ms // 1000 * 4

    async with websockets.connect(url, max_size=None, open_timeout=25) as audio_ws:
        # No `channels` field: see ws_stream_probe._stream_wav.
        await audio_ws.send(json.dumps({
            "callEvent": "START", "callId": call_id,
            "fromNumber": "Desktop Video Probe", "toNumber": "LMA System",
            "agentId": agent_id, "samplingRate": SAMPLE_RATE,
        }))
        t0 = time.monotonic()

        async def audio() -> None:
            for i, pos in enumerate(range(0, len(media.pcm), chunk_bytes)):
                await audio_ws.send(media.pcm[pos:pos + chunk_bytes])
                delay = t0 + (i + 1) * chunk_ms / 1000 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

        async def video() -> None:
            # The app opens its video socket after the audio one, and reports
            # how far into the audio its first frame is.
            await asyncio.sleep(LEAD_SECONDS / 2)
            async with websockets.connect(url, max_size=None, open_timeout=25) as video_ws:
                await video_ws.send(json.dumps({
                    "callEvent": "START_VIDEO", "callId": call_id,
                    "videoTimeOffsetMs": int(LEAD_SECONDS * 1000),
                }))
                init, frags = media.fragments[0], media.fragments[1:]
                for i, frag in enumerate(frags):
                    # A fragment is delivered once its two seconds have been
                    # captured; the init segment arrives with the first one.
                    due = t0 + LEAD_SECONDS + (i + 1) * KEYFRAME_SECONDS
                    delay = due - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if i == 0:
                        await video_ws.send(init)
                        observed["video_bytes"] += len(init)
                    await video_ws.send(frag)
                    observed["fragments_sent"] += 1
                    observed["video_bytes"] += len(frag)
                observed["video_state"] = video_ws.state.name
                await video_ws.send(json.dumps({"callEvent": "END_VIDEO", "callId": call_id}))
                await asyncio.sleep(1.0)
            observed["video_close"] = video_ws.close_code

        await asyncio.gather(audio(), video())
        observed["audio_state"] = audio_ws.state.name
        await audio_ws.send(json.dumps({"callEvent": "END", "callId": call_id}))
        await asyncio.sleep(1.0)
    return observed


# ── verification ───────────────────────────────────────────────────────────

def _query_all(ddb, table: str, pk: str) -> list[dict]:
    items: list[dict] = []
    kwargs = {
        "TableName": table,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": pk}},
    }
    while True:
        resp = ddb.query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _plain(item: dict) -> dict:
    from boto3.dynamodb.types import TypeDeserializer

    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in item.items()}


def s3_location(url: str) -> tuple[str, str]:
    """(bucket, key) of a virtual-hosted S3 https URL with an encoded key."""
    parsed = urllib.parse.urlparse(url)
    bucket = parsed.netloc.split(".s3.", 1)[0]
    return bucket, urllib.parse.unquote(parsed.path.lstrip("/"))


def read_keyframes(ddb, table: str, call_id: str) -> list[dict]:
    frames = [_plain(i) for i in _query_all(ddb, table, f"kf#{call_id}")]
    for f in frames:
        for k in ("Timestamp", "VideoOffset", "ChangeScore"):
            if k in f and f[k] is not None:
                f[k] = float(f[k])
    return sorted(frames, key=lambda f: f.get("FrameId", ""))


def check_images(s3, frames: list[dict]) -> list[dict]:
    """HEAD both images of every frame; returns one record per image."""
    out = []
    for f in frames:
        for which in ("ImageUrl", "ThumbnailUrl"):
            url = f.get(which, "")
            bucket, key = s3_location(url) if url else ("", "")
            rec = {"frame": f.get("FrameId"), "which": which, "bucket": bucket, "key": key,
                   "exists": False, "content_type": None, "bytes": 0}
            if bucket and key:
                try:
                    head = s3.head_object(Bucket=bucket, Key=key)
                    rec.update(exists=True, content_type=head.get("ContentType"),
                               bytes=head.get("ContentLength", 0))
                except Exception as err:  # noqa: BLE001
                    rec["error"] = str(err)
            out.append(rec)
    return out


def run_probe(stack_name: str, region: str | None = None, media: Media | None = None,
              keyframe_wait_seconds: float = 90.0) -> dict:
    session = boto3.Session(region_name=region)
    region = session.region_name
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")
    s3 = session.client("s3")
    outs = _outputs(cfn, stack_name)

    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    username, password = credentials
    client_id = outs["CognitoUserPoolClientId"]
    pool_id = outs.get("MCPServerUserPoolId") or outs.get(
        "CognitoUserPoolTokenIssuerUrl", "").rsplit("/", 1)[-1]
    tokens = _mint_tokens(region, pool_id, client_id, username, password)

    media = media or build_media(region)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    call_id = f"desktop-video-probe-{stamp}"
    table = _event_sourcing_table(cfn, stack_name)

    observed = asyncio.run(_stream(outs["LMAWebsocketEndpoint"], tokens, call_id, username, media))

    result: dict = {
        "call_id": call_id, "username": username, "table": table,
        "recordings_bucket": outs.get("RecordingsS3Bucket"),
        "slides": [s.title for s in media.slides],
        "expected_timestamps": media.expected_timestamps(),
        "durations": media.durations, "fragments": len(media.fragments) - 1,
        **observed,
        "meeting_created": False, "segment_count": 0, "keyframes": [], "images": [],
    }
    deadline = time.monotonic() + keyframe_wait_seconds
    while time.monotonic() < deadline:
        if not result["meeting_created"]:
            result["meeting_created"] = _meeting_exists(ddb, table, call_id)
        result["segment_count"] = len(_query_all(ddb, table, f"trs#{call_id}"))
        result["keyframes"] = read_keyframes(ddb, table, call_id)
        if (result["meeting_created"] and result["segment_count"] > 0
                and len(result["keyframes"]) >= len(media.slides)):
            # A frame or two more would be a detector finding; give it a moment.
            time.sleep(5)
            result["keyframes"] = read_keyframes(ddb, table, call_id)
            break
        time.sleep(3)
    result["images"] = check_images(s3, result["keyframes"])
    return result


# ── meeting assistant ──────────────────────────────────────────────────────

def assistant_function_name(stack_name: str) -> str:
    return f"STRANDS-{stack_name}-MeetingAssist"


def assistant_has_frames_tool(stack_name: str, region: str | None = None) -> bool:
    """Whether the deployed assistant's code includes the shared-screen tool.

    Read from the function's own package rather than inferred from the stack's
    version, so a stack updated from a branch without the tool reports honestly.
    """
    import urllib.request

    lam = boto3.Session(region_name=region).client("lambda")
    loc = lam.get_function(FunctionName=assistant_function_name(stack_name))["Code"]["Location"]
    with urllib.request.urlopen(loc, timeout=60) as resp:  # nosec B310 - presigned https URL from GetFunction
        package = zipfile.ZipFile(io.BytesIO(resp.read()))
    return any(Path(n).name == "meeting_frames.py" for n in package.namelist())


def ask_assistant(stack_name: str, call_id: str, user_email: str, table: str,
                  question: str, region: str | None = None) -> dict:
    from botocore.config import Config

    lam = boto3.Session(region_name=region).client(
        "lambda", config=Config(read_timeout=310, retries={"max_attempts": 0}))
    resp = lam.invoke(
        FunctionName=assistant_function_name(stack_name),
        Payload=json.dumps({
            "text": question, "call_id": call_id, "conversation_history": [],
            "userEmail": user_email, "dynamodb_table_name": table,
        }).encode(),
    )
    body = json.loads(resp["Payload"].read() or b"{}")
    return {"function_error": resp.get("FunctionError"), "response": body,
            "message": body.get("message", "") if isinstance(body, dict) else ""}


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("stack", nargs="?", default="LMA")
    ap.add_argument("--region")
    ap.add_argument("--media-only", metavar="DIR",
                    help="build the slides, narration and fMP4 into DIR and stop")
    args = ap.parse_args(argv)

    if args.media_only:
        media = build_media(args.region, Path(args.media_only))
        ffmpeg = find_ffmpeg()
        print(json.dumps({
            "dir": str(media.workdir), "durations": media.durations,
            "audio_seconds": round(media.audio_seconds, 2),
            "fragments": len(media.fragments) - 1,
            "fragment_bytes": [len(f) for f in media.fragments],
            "keyframe_times": probe_keyframes(ffmpeg, media.mp4_path),
        }, indent=2))
        return 0

    res = run_probe(args.stack, region=args.region)
    print(json.dumps(res, indent=2, default=str))
    if assistant_has_frames_tool(args.stack, args.region):
        ans = ask_assistant(args.stack, res["call_id"], res["username"], res["table"],
                            "What did the last slide say?", args.region)
        print(json.dumps(ans, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
