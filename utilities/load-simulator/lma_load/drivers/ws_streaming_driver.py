# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""WebSocket streaming driver.

Reproduces exactly what the LMA browser client does when a user clicks
"Start Streaming": opens a WSS connection to LMA's transcriber endpoint with
the user's Cognito JWT, sends a ``START`` JSON frame, streams binary audio
frames in real-time pacing, then sends ``END``.

This is the **high-fidelity** concurrent-meetings driver: every meeting
spawns a real Amazon Transcribe *streaming* session on the server side, so
this is the only driver that will genuinely surface the Transcribe
concurrent-streaming quota ceiling.

The audio source is a stereo WAV fixture (shipped; operator-overridable).
It is decoded once on startup, then each concurrent meeting re-streams the
same PCM chunks at real-time pacing, looping if ``duration_s`` exceeds the
clip length.

Run cost scales with meetings × duration (stereo) at current streaming list
prices — ``quota_probe`` prints an estimate before the run starts.

Reference:

* TypeScript reference client: ``utilities/websocket-client/src/index.ts``
* LMA browser-side implementation: ``lma-ai-stack/source/ui/src/components/embed/EmbedStreamAudio.jsx``
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any


from lma_load.auth.cognito import SyntheticUserPool
from lma_load.run_context import RunContext
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)

# Pacing constants that mirror the browser client.
CHUNK_SIZE_MS = 200   # frames every 200 ms (matches ui client)
DEFAULT_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


# ---------------------------------------------------------------------------
# WAV fixture loader
# ---------------------------------------------------------------------------
@dataclass
class _LoadedWav:
    sample_rate: int
    channels: int
    frames: bytes           # interleaved PCM16 bytes
    chunk_bytes: int        # frames-per-chunk × channels × 2


def _load_wav(path: Path) -> _LoadedWav:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        if sw != BYTES_PER_SAMPLE:
            raise ValueError(
                f"{path}: sample-width {sw} not supported; please supply a 16-bit WAV"
            )
        data = wf.readframes(wf.getnframes())
    frames_per_chunk = int(sr * CHUNK_SIZE_MS / 1000)
    chunk_bytes = frames_per_chunk * ch * sw
    logger.info(
        "Loaded WAV %s: %d Hz, %d channels, %d bytes, chunk=%d bytes",
        path, sr, ch, len(data), chunk_bytes,
    )
    return _LoadedWav(sample_rate=sr, channels=ch, frames=data, chunk_bytes=chunk_bytes)


# Default fixture preference order:
#   1. ``stereo-demo-call.wav``  — real two-speaker call recording (ch_0 = caller,
#      ch_1 = agent). Produces meaningful transcripts when LMA enables Transcribe
#      ``ChannelIdentification`` on the stereo stream.
#   2. ``stereo-16k-30s.wav``    — synthetic 220 Hz / 330 Hz tone bursts. Useful
#      only as a connectivity / quota-ceiling smoke test; will *not* produce a
#      meaningful transcript or summary because there's no speech in the audio.
_DEFAULT_FIXTURE_CANDIDATES = (
    "stereo-demo-call.wav",
    "stereo-16k-30s.wav",
)


def _resolve_wav(override: str | None) -> _LoadedWav:
    """Prefer the operator's ``--wav``; otherwise fall back to the shipped fixture.

    Tries ``stereo-demo-call.wav`` first (real two-speaker speech, transcribes
    cleanly when the WSS stack enables ``ChannelIdentification``), then the
    legacy synthetic tone fixture, then errors if neither is available.
    """
    if override:
        return _load_wav(Path(override).expanduser().resolve())
    last_err: Exception | None = None
    for fixture in _DEFAULT_FIXTURE_CANDIDATES:
        try:
            with resources.as_file(
                resources.files("lma_load").joinpath("fixtures", fixture)
            ) as path:
                if not path.exists():
                    continue
                return _load_wav(path)
        except (FileNotFoundError, ModuleNotFoundError) as err:
            last_err = err
            continue
    raise RuntimeError(
        "No --wav provided and no shipped fixture could be loaded "
        f"(tried: {', '.join(_DEFAULT_FIXTURE_CANDIDATES)}). Pass --wav <path> "
        f"to a stereo 16-bit PCM WAV file. Underlying error: {last_err}"
    )



# ---------------------------------------------------------------------------
# One-meeting coroutine
# ---------------------------------------------------------------------------
async def _stream_one(
    idx: int,
    ws_url: str,
    wav: _LoadedWav,
    jwt_access: str,
    jwt_id: str,
    duration_s: float,
    call_prefix: str,
    agent_label: str,
) -> dict[str, Any]:
    """Open a WSS connection, stream audio for ``duration_s``, then END."""
    import websockets  # optional-dep; imported lazily

    # Guard against an older ``websockets`` (<12) satisfying the install — the
    # ``additional_headers=`` kwarg used below is only honoured on >=12, and
    # older releases silently forward it into ``asyncio.create_connection``
    # which then raises a cryptic TypeError about an unexpected keyword.
    _ws_ver = tuple(int(p) for p in websockets.__version__.split(".")[:2] if p.isdigit())
    if _ws_ver and _ws_ver < (12, 0):
        raise RuntimeError(
            f"websockets=={websockets.__version__} is too old for the LMA WS driver "
            "(need >=12.0 for additional_headers=). Upgrade with:\n"
            "    pip install -U 'websockets>=12.0'\n"
            "Tip: make sure you're upgrading the same Python env that ships the "
            "'lma' entry point (check `which lma` and its shebang)."
        )


    call_id = f"{call_prefix}-{idx:05d} - {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}"
    metadata = {
        "callId": call_id,
        "fromNumber": "LoadSim-Customer",
        "toNumber": "LoadSim-System",
        "agentId": agent_label,
        "samplingRate": wav.sample_rate,
        "callEvent": "START",
    }
    # LMA's JWT-verifier accepts the token from either a custom header
    # *or* a query-string parameter (see lma-websocket-transcriber-stack/
    # source/app/src/utils/jwt-verifier.ts: ``query.authorization ||
    # headers.authorization``). The WSS endpoint is fronted by CloudFront,
    # which strips unknown request headers (including ``authorization``
    # on a WebSocket upgrade), so header-based auth returns 401 ``Error
    # from cloudfront`` before the request ever reaches Fastify. The real
    # browser client works around this by passing the tokens as query
    # parameters (see ``EmbedStreamAudio.jsx``'s ``queryParams``), so we
    # do the same.
    qs = urllib.parse.urlencode(
        {
            "authorization": f"Bearer {jwt_access}",
            "id_token": jwt_id or "",
            "refresh_token": "",
        }
    )
    sep = "&" if "?" in ws_url else "?"
    auth_url = f"{ws_url}{sep}{qs}"
    # Also send the tokens as headers — harmless (CloudFront strips them)
    # and useful for non-CloudFront-fronted deployments.
    headers = {
        "authorization": f"Bearer {jwt_access}",
        "id_token": jwt_id or "",
        "refresh_token": "",
    }
    t0 = time.monotonic()
    sent_bytes = 0

    try:
        async with websockets.connect(
            auth_url,
            additional_headers=headers,   # websockets >= 12
            max_size=None,
            ping_interval=30,
            ping_timeout=30,
            close_timeout=5,
        ) as ws:

            # 1) START
            await ws.send(json.dumps(metadata))
            t_first_send = time.monotonic()
            # 2) real-time audio
            deadline = time.monotonic() + duration_s
            chunk_size = wav.chunk_bytes
            if chunk_size <= 0:
                chunk_size = wav.sample_rate * BYTES_PER_SAMPLE * wav.channels // 5
            while time.monotonic() < deadline:
                for offset in range(0, len(wav.frames), chunk_size):
                    if time.monotonic() >= deadline:
                        break
                    chunk = wav.frames[offset : offset + chunk_size]
                    await ws.send(chunk)
                    sent_bytes += len(chunk)
                    # Pace to real time. Small drift is fine — Transcribe streams
                    # are forgiving up to ~30s of buffering.
                    await asyncio.sleep(CHUNK_SIZE_MS / 1000.0)
            # 3) END
            await ws.send(json.dumps({**metadata, "callEvent": "END"}))
    except Exception as err:  # noqa: BLE001
        return {
            "callId": call_id,
            "status": "error",
            "error": str(err),
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "bytes_sent": sent_bytes,
        }

    return {
        "callId": call_id,
        "status": "ok",
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
        "first_send_latency_ms": round((t_first_send - t0) * 1000, 1),
        "bytes_sent": sent_bytes,
    }


# ---------------------------------------------------------------------------
# Driver entry point (called by scenarios.concurrent)
# ---------------------------------------------------------------------------
async def drive_websocket(ctx: RunContext, stack: LMAStackInfo, params) -> list[dict]:
    """Public driver entry — called with (ctx, stack, ConcurrentParams)."""
    if not params.email_prefix or not params.email_domain:
        raise RuntimeError(
            "--email-prefix and --email-domain are required for the websocket driver "
            "(synthetic Cognito users are minted so each meeting has a real JWT)."
        )

    # 1) Mint synthetic users — one per worker if user_pool_size == 0.
    user_count = max(1, params.user_pool_size or min(params.concurrency, params.meetings))
    pool = SyntheticUserPool(
        user_pool_id=stack.user_pool_id,
        user_pool_client_id=stack.user_pool_client_id,
        region=ctx.region,
        run_id=ctx.run_id,
        profile=ctx.profile,
    )
    pool.provision(
        count=user_count,
        email_prefix=params.email_prefix,
        email_domain=params.email_domain,
    )
    pool.authenticate_all()
    users_with_tokens = [u for u in pool.users if u.access_token]
    if not users_with_tokens:
        raise RuntimeError(
            "No synthetic users could be authenticated. Check Cognito USER_PASSWORD_AUTH "
            "is enabled on the app client."
        )
    logger.info("Authenticated %d synthetic users for the WS driver", len(users_with_tokens))

    wav = _resolve_wav(params.wav_path)
    semaphore = asyncio.Semaphore(params.concurrency)
    rng = random.Random(ctx.run_id)

    async def _bounded(idx: int) -> dict:
        # Ramp-in + jitter
        await asyncio.sleep(
            (idx / max(params.meetings, 1)) * params.ramp_s
            + rng.random() * params.jitter_s
        )
        async with semaphore:
            user = users_with_tokens[(idx - 1) % len(users_with_tokens)]
            return await _stream_one(
                idx=idx,
                ws_url=stack.ws_endpoint,
                wav=wav,
                jwt_access=user.access_token,
                jwt_id=user.id_token,
                duration_s=params.duration_s,
                call_prefix=f"loadtest-{ctx.run_id}-ws",
                agent_label=user.email,
            )

    tasks = [asyncio.create_task(_bounded(i)) for i in range(1, params.meetings + 1)]
    out: list[dict] = []
    for t in asyncio.as_completed(tasks):
        try:
            out.append(await t)
        except Exception as err:  # noqa: BLE001
            logger.exception("ws driver meeting failed: %s", err)
            out.append({"status": "error", "error": str(err)})
    return out
