#!/usr/bin/env python3
"""WebSocket-through-MicroVM smoke test.

Connects to a **live, RUNNING MicroVM's** dedicated HTTPS endpoint over WSS using
the **three MicroVM subprotocols** (API doc §4):

    lambda-microvms                              (REQUIRED base protocol)
    lambda-microvms.authentication.<auth-token>  (the JWE token from the launch)
    lambda-microvms.port.8080                     (target the ASR server's port)

then streams 16 kHz/16-bit/mono PCM — the LIVE local mic (``--microphone``), a WAV
file (``--wav``), or a synthetic tone if neither is given — and asserts the
end-to-end ASR trajectory over the real MicroVM endpoint:
``ready`` → ``partial*`` → ``final`` → ``termination`` (design §5). Exit 0 on a
clean trajectory, non-zero with a diagnostic otherwise — a headless smoke test,
not a UI (matches ``scripts/demo_client.py``'s client-only role).

This command validates a live MicroVM endpoint end to end. It needs a fresh
endpoint and auth token, so run it after the image build, MicroVM launch, and
token-creation steps in ``DEPLOY.md``. The pure trajectory validator
(:func:`classify_messages`) is unit-tested offline in
``tests/test_microvm_ws_smoke.py``.

Usage (on your authenticated machine, against a RUNNING MicroVM):
    # endpoint = run-microvm's "endpoint"; token = create-microvm-auth-token's authToken
    python scripts/microvm_ws_smoke.py \\
        --endpoint mvm-0123.lambda-microvm.us-east-1.on.aws \\
        --token "<jwe-token>" \\
        [--microphone | --wav sample.wav] [--port 8080] [--insecure]

The ``--microphone`` mode streams your LIVE local mic (16 kHz mono, captured via
ffmpeg — the repo's audio tool) over the real MicroVM WSS, printing partials →
finals until Ctrl-C: the interactive "talk to my MicroVM" experience.

You can also let the router mint everything and pass a full wss:// URL via
``--url`` instead of ``--endpoint`` (the subprotocols still carry the token).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import signal
import struct
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8080


def websocket_subprotocols(token: str, *, port: int = DEFAULT_PORT) -> list[str]:
    """Build the three WebSocket subprotocols for the upgrade.

    Base protocol first (required), then the port-scoped auth token, then the
    target port. Browsers cannot set handshake headers, so auth rides on the
    subprotocol list; the MicroVM proxy strips all three before forwarding the
    upgrade to the ASR server. Kept in lock-step with the transcriber's
    ``subprotocols()`` in ``calleventdata/asr-microvm.ts`` and the web UI's
    ``vncConnection.js``.
    """
    return [
        "lambda-microvms",
        f"lambda-microvms.authentication.{token}",
        f"lambda-microvms.port.{port}",
    ]

_BYTES_PER_SAMPLE = 2
_MIC_RATE = 16000


# --- Pure trajectory validation (unit-tested offline) -----------------------


@dataclass
class SmokeResult:
    """Outcome of validating a server->client message trajectory (design §5).

    ``ok`` is the acceptance verdict: a ``ready`` was seen, at least one ``final``
    arrived, and a ``termination`` closed the session, with no fatal ``error``.
    The counters + ``reasons`` make a failure diagnosable at a glance.
    """

    ok: bool = False
    ready: bool = False
    partials: int = 0
    finals: int = 0
    termination: bool = False
    audio_seconds: float | None = None
    segments: int | None = None
    session_id: str | None = None
    errors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def classify_messages(messages: list[dict[str, object]]) -> SmokeResult:
    """Validate a decoded server->client trajectory against the wire contract.

    Enforces the design §5 ordering the acceptance check names: a ``ready`` MUST
    precede any transcript; at least one ``final`` and a closing ``termination``
    MUST appear; a fatal ``error`` fails the smoke. Pure (no I/O) so the state
    machine is unit-tested without a socket.
    """
    result = SmokeResult()
    for msg in messages:
        kind = msg.get("type")
        if kind == "ready":
            result.ready = True
            sid = msg.get("session_id")
            result.session_id = str(sid) if sid is not None else None
        elif kind == "partial":
            if not result.ready:
                result.reasons.append("partial arrived before ready")
            result.partials += 1
        elif kind == "final":
            if not result.ready:
                result.reasons.append("final arrived before ready")
            result.finals += 1
        elif kind == "termination":
            result.termination = True
            audio = msg.get("audio_seconds")
            segs = msg.get("segments")
            result.audio_seconds = float(audio) if isinstance(audio, int | float) else None
            result.segments = int(segs) if isinstance(segs, int | float) else None
        elif kind == "error":
            result.errors.append(str(msg.get("message", "")))
            if msg.get("fatal"):
                result.reasons.append(f"fatal error: {msg.get('code')}")

    if not result.ready:
        result.reasons.append("no ready message received")
    if result.finals == 0:
        result.reasons.append("no final transcript received")
    if not result.termination:
        result.reasons.append("no termination summary received")
    result.ok = (
        result.ready
        and result.finals > 0
        and result.termination
        and not result.errors
        and not result.reasons
    )
    return result


# --- Audio source -----------------------------------------------------------


def _read_pcm(path: Path) -> tuple[bytes, int]:
    """Read a mono 16-bit PCM WAV → ``(raw_bytes, sample_rate)`` (fails loudly)."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != _BYTES_PER_SAMPLE:
        raise SystemExit(f"{path}: need 16-bit PCM, got {width * 8}-bit.")
    if channels != 1:
        raise SystemExit(f"{path}: need mono, got {channels} channels.")
    return frames, rate


def _synthetic_pcm(duration_ms: int = 1500, sample_rate: int = _MIC_RATE) -> bytes:
    """A deterministic tone to stream when no WAV is given (smoke only needs bytes).

    A real recogniser may transcribe a bare tone as empty text — the smoke's job
    is to prove the WSS-through-MicroVM PATH (handshake + streaming + trajectory),
    so pass a real ``--wav`` when you also want to assert transcription content.
    """
    n = int(sample_rate * duration_ms / 1000)
    step = 2.0 * math.pi * 220.0 / sample_rate
    return struct.pack("<" + "h" * n, *(int(8000 * math.sin(step * i)) for i in range(n)))


async def _stream_microphone(
    ws: object, rate: int, frame_ms: int, stop: asyncio.Event
) -> None:
    """Capture the live OS mic and stream it as binary PCM frames until ``stop``.

    Reuses ``scripts/demo_client.py``'s proven capture path so the two clients stay
    consistent: ffmpeg's ``avfoundation`` device (macOS) emits exactly the server's
    format — 16 kHz / mono / signed-16-bit LE — so there's no client-side resample,
    and reading fixed ``frame_ms`` chunks off ffmpeg stdout paces the stream to real
    time naturally (samples arrive as the mic produces them). ffmpeg is the repo's
    audio tool (already used for ``--wav``), so this adds no PortAudio/native binding.
    On ``stop`` (Ctrl-C) we terminate capture and send ``eos`` so the server flushes
    its final transcript(s) + termination summary.
    """
    frame_bytes = (rate * frame_ms // 1000) * _BYTES_PER_SAMPLE
    # ``:default`` selects the OS default input device; -nostdin so ffmpeg doesn't
    # fight us for the terminal; raw s16le on stdout is exactly what the server wants.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-f", "avfoundation",
        "-i", ":default",
        "-ac", "1",
        "-ar", str(rate),
        "-f", "s16le",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise SystemExit(
            "microphone capture needs 'ffmpeg' on PATH (it is also used for "
            "--wav). Install it (macOS: brew install ffmpeg) and retry."
        ) from None

    assert proc.stdout is not None
    # Diagnostic (client side): track what the mic actually produced and what we sent,
    # so a "connected but no transcription" run distinguishes (a) the mic captured
    # nothing/near-silence from a downstream server problem. ``max_abs`` is the peak
    # 16-bit sample seen — near 0 means the mic delivered digital silence even if
    # bytes flowed (wrong input device, muted, or missing OS mic permission).
    frames_sent = 0
    bytes_sent = 0
    max_abs = 0
    last_tick = 0.0
    loop = asyncio.get_running_loop()
    try:
        while not stop.is_set():
            # Read one frame's worth; readexactly gives a steady frame_ms cadence.
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.readexactly(frame_bytes), timeout=0.5
                )
            except TimeoutError:
                # No full frame yet (mic warming up / silence buffering) — re-check
                # the stop flag and keep waiting rather than blocking forever.
                continue
            except asyncio.IncompleteReadError as exc:
                # ffmpeg exited: stream whatever partial frame it left, then stop.
                if exc.partial:
                    await ws.send(exc.partial)  # type: ignore[attr-defined]
                    frames_sent += 1
                    bytes_sent += len(exc.partial)
                break
            await ws.send(chunk)  # type: ignore[attr-defined]
            frames_sent += 1
            bytes_sent += len(chunk)
            # Track peak amplitude to tell "silence" apart from "no bytes".
            samples = struct.unpack("<" + "h" * (len(chunk) // _BYTES_PER_SAMPLE), chunk)
            if samples:
                max_abs = max(max_abs, max(abs(s) for s in samples))
            # ~1s heartbeat so the operator sees audio flowing (or NOT) in real time.
            now = loop.time()
            if now - last_tick >= 1.0:
                last_tick = now
                print(
                    f"  mic: sent {frames_sent} frames / {bytes_sent} bytes "
                    f"(about {bytes_sent / _BYTES_PER_SAMPLE / rate:.1f}s), peak={max_abs}",
                    file=sys.stderr,
                )
    finally:
        # Always summarise what was captured/sent — the first thing to read when a run
        # produced no transcription. Zero bytes ⇒ mic sent nothing (hyp a); bytes but
        # peak≈0 ⇒ digital silence (check device/permission/mute) — both are client-side.
        print(
            f"\n  mic total: {frames_sent} frames / {bytes_sent} bytes "
            f"(about {bytes_sent / _BYTES_PER_SAMPLE / rate:.1f}s audio), "
            f"peak amplitude={max_abs}",
            file=sys.stderr,
        )
        if bytes_sent == 0:
            print("  warning: microphone capture produced no audio.", file=sys.stderr)
        elif max_abs < 32:  # ~ -60 dBFS: effectively digital silence
            print(
                "  warning: microphone audio is effectively silent. Check the input "
                "device, mute switch, and OS microphone permission.",
                file=sys.stderr,
            )
        # Stop capture cleanly, then close the stream with eos so the server can
        # flush the final transcript(s) and send its termination summary.
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
        # Surface a genuine ffmpeg startup failure (e.g. mic permission denied)
        # instead of silently ending with no audio.
        if proc.returncode not in (0, None) and stop.is_set() is False:
            err = b""
            if proc.stderr is not None:
                try:
                    err = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                except TimeoutError:
                    err = b""
            detail = err.decode(errors="replace").strip()
            raise SystemExit(
                "ffmpeg microphone capture failed"
                + (f":\n{detail}" if detail else " (no stderr).")
                + "\nOn macOS, grant microphone access to your terminal in "
                "System Settings → Privacy & Security → Microphone."
            )
        await ws.send(json.dumps({"type": "eos"}))  # type: ignore[attr-defined]


# --- Live smoke (operator-run) ----------------------------------------------


def build_url(endpoint: str | None, url: str | None, *, insecure: bool) -> str:
    """Resolve the WSS URL from either a raw ``--url`` or a MicroVM ``--endpoint``.

    A MicroVM endpoint is a bare host (API doc §3.3: ``mvm-….on.aws``); wrap it as
    ``wss://<host>/`` (``ws://`` only with ``--insecure`` for a local proxy test).
    """
    if url:
        return url
    if not endpoint:
        raise SystemExit("one of --endpoint or --url is required.")
    scheme = "ws" if insecure else "wss"
    host = endpoint.removeprefix("https://").removeprefix("wss://").rstrip("/")
    return f"{scheme}://{host}/"


async def _run_live(args: argparse.Namespace) -> int:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    # Mic capture is hard-wired to 16 kHz (the server's pipeline rate); a WAV
    # streams at its own rate; the synthetic tone is 16 kHz.
    if args.microphone:
        pcm, source_rate = b"", _MIC_RATE
    elif args.wav:
        pcm, source_rate = _read_pcm(Path(args.wav))
    else:
        pcm, source_rate = _synthetic_pcm(), _MIC_RATE
    rate = args.sample_rate or source_rate
    if args.sample_rate and args.sample_rate != source_rate:
        print(
            f"note: --sample-rate {args.sample_rate} differs from the "
            f"{'capture' if args.microphone else 'source'}'s {source_rate} Hz; "
            "the server will resample.",
            file=sys.stderr,
        )

    url = build_url(args.endpoint, args.url, insecure=args.insecure)
    subprotocols = websocket_subprotocols(args.token, port=args.port)
    frame_bytes = (rate * args.frame_ms // 1000) * _BYTES_PER_SAMPLE

    print(f"connecting {url}")
    print(
        f"subprotocols: {subprotocols[0]}, "
        f"authentication.<token>, port.{args.port}"
    )
    if args.microphone:
        print(f"streaming live microphone ({rate} Hz mono). Speak now; Ctrl-C to stop.\n")

    received: list[dict[str, object]] = []
    # Config on the query string is not settable once we pass a bare URL, so send
    # it as the first text frame instead (the server merges query + first frame).
    config_frame = json.dumps(
        {"type": "config", "sample_rate": rate, "interim_results": True}
    )
    try:
        async with connect(url, subprotocols=subprotocols, max_size=None) as ws:  # type: ignore[arg-type]
            await ws.send(config_frame)

            print("sent config frame; awaiting ready.", file=sys.stderr)

            async def _reader() -> None:
                # Diagnostic (client side): count every server frame so a run shows
                # whether ANYTHING came back after ``ready``. If the server end-of-
                # session log reports messages_emitted>0 but this stays at just the
                # ready frame, the loss is in transport between server and client.
                frames_recv = 0
                try:
                    async for raw in ws:
                        frames_recv += 1
                        msg = json.loads(raw)
                        received.append(msg)
                        _print_message(msg)
                        if msg.get("type") == "termination":
                            return
                except ConnectionClosed as exc:
                    print(
                        f"\nwarning: server closed the connection after {frames_recv} "
                        f"frame(s): {exc}",
                        file=sys.stderr,
                    )
                    return

            reader = asyncio.create_task(_reader())
            if args.microphone:
                # Ctrl-C (SIGINT) sets the stop event; capture then flushes eos and
                # we drain the reader for the final transcript(s) + termination.
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                # Fallback for platforms without add_signal_handler; the
                # KeyboardInterrupt handler in main() still stops us cleanly.
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(signal.SIGINT, stop.set)
                try:
                    await _stream_microphone(ws, rate, args.frame_ms, stop)
                finally:
                    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                        loop.remove_signal_handler(signal.SIGINT)
            else:
                # WAV / synthetic path: report exactly how many bytes we streamed so
                # a no-transcription run can be checked against the server's bytes_in.
                sent = 0
                for i in range(0, len(pcm), frame_bytes):
                    frame = pcm[i : i + frame_bytes]
                    await ws.send(frame)
                    sent += len(frame)
                    await asyncio.sleep(args.frame_ms / 1000.0 if not args.fast else 0)
                print(
                    f"  streamed {sent} bytes (about {sent / _BYTES_PER_SAMPLE / rate:.1f}s "
                    f"audio) then eos",
                    file=sys.stderr,
                )
                await ws.send(json.dumps({"type": "eos"}))
            await asyncio.wait_for(reader, timeout=args.recv_timeout)
    except TimeoutError:
        print("\nerror: timed out waiting for termination.", file=sys.stderr)
    except OSError as exc:
        print(f"\nerror: could not connect to {url}: {exc}", file=sys.stderr)
        print(
            "  Is the MicroVM RUNNING and the token unexpired + port-scoped to "
            f"{args.port}? (create-microvm-auth-token, API doc §3.5)",
            file=sys.stderr,
        )
        return 2

    result = classify_messages(received)
    _print_verdict(result)
    return 0 if result.ok else 1


def _print_message(msg: dict[str, object]) -> None:
    kind = msg.get("type")
    if kind == "ready":
        print(f"ready session={str(msg.get('session_id'))[:12]}")
    elif kind == "partial":
        sys.stdout.write(f"\rpartial [{msg.get('segment')}] {msg.get('text')}")
        sys.stdout.flush()
    elif kind == "final":
        print(f"\rfinal [{msg.get('segment')}] {msg.get('text')}")
    elif kind == "termination":
        print(f"\ntermination audio={msg.get('audio_seconds')}s segments={msg.get('segments')}")
    elif kind == "error":
        print(f"\nerror [{msg.get('code')}] {msg.get('message')}", file=sys.stderr)


def _print_verdict(result: SmokeResult) -> None:
    print("\n--- smoke verdict ---")
    print(f"  ready={result.ready} partials={result.partials} finals={result.finals} "
          f"termination={result.termination}")
    if result.ok:
        print("  PASS: received ready, partials, finals, and termination.")
    else:
        print("  FAIL:")
        for reason in result.reasons:
            print(f"    - {reason}")
        for err in result.errors:
            print(f"    - server error: {err}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--endpoint", help="MicroVM endpoint host (run-microvm 'endpoint')")
    src.add_argument("--url", help="full wss:// URL (alternative to --endpoint)")
    p.add_argument(
        "--token", required=True, help="auth token (create-microvm-auth-token 'authToken')"
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="target port (default 8080)")
    audio = p.add_mutually_exclusive_group()
    audio.add_argument("--wav", help="16 kHz/16-bit/mono WAV to stream (default: synthetic tone)")
    audio.add_argument(
        "--microphone",
        "--mic",
        dest="microphone",
        action="store_true",
        help="stream the LIVE OS microphone (16 kHz mono via ffmpeg) until Ctrl-C",
    )
    p.add_argument(
        "--sample-rate",
        type=int,
        default=0,
        help="override the sample rate sent in config (default: the audio source's rate)",
    )
    p.add_argument("--frame-ms", type=int, default=100, help="PCM frame size in ms (default 100)")
    p.add_argument("--fast", action="store_true", help="don't pace to real time")
    p.add_argument("--insecure", action="store_true", help="use ws:// (local proxy test only)")
    p.add_argument("--recv-timeout", type=float, default=60.0, help="seconds to await termination")
    args = p.parse_args(argv)
    # Mic capture is hard-wired to 16 kHz; reject a conflicting config override so
    # we don't lie to the server about the stream format.
    if args.microphone and args.sample_rate not in (0, _MIC_RATE):
        p.error(
            f"microphone mode captures at {_MIC_RATE} Hz only; "
            f"drop --sample-rate or set it to {_MIC_RATE}."
        )
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run_live(_parse_args(argv)))
    except KeyboardInterrupt:
        # Fallback when the asyncio SIGINT handler wasn't installed (mic mode):
        # don't dump a traceback — Ctrl-C is the intended way to stop the mic.
        print("\nstopped (Ctrl-C).", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
