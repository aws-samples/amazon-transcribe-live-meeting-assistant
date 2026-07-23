# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""End-to-end WebSocket audio streaming probe (the transcriber front door).

Connects to the live transcriber WebSocket exactly like the browser Stream Audio
tab / the utilities/websocket-client does — mints a real Cognito access token
(SRP), opens wss://.../api/v1/ws, sends a START control message, streams a REAL
stereo WAV file at real-time pace, sends END, then verifies the full pipeline:

    WS upgrade holds  ->  Amazon Transcribe produces segments  ->  Kinesis  ->
    CallEventProcessor  ->  DynamoDB (meeting row `c#<id>` + transcript segments
    `trs#<id>`).

This exercises, in one shot, every regression this batch hit:
  * @fastify/websocket 11 upgrade ordering (a 500 here => connection drops)
  * fastify 5 boot / audio handling
  * gql 3.5 <-> AppSync (a gql-4 break => no meeting / no segments written)

Requires a Cognito user's credentials via env: LMA_TEST_USERNAME / LMA_TEST_PASSWORD.
Uses a bundled real WAV (utilities/load-simulator/lma_load/fixtures/stereo-16k-30s.wav)
by default; override with LMA_TEST_WAV.

Depends on `pycognito` and `websockets` (see integ-tests/requirements.txt). The
test that uses this probe skips if they're absent.

Standalone:
    LMA_TEST_USERNAME=you@x LMA_TEST_PASSWORD=... \
      AWS_PROFILE=default python integ-tests/ws_stream_probe.py lma-integtest1
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import boto3

# Default real audio fixture: a two-speaker call recording (8 kHz stereo) that
# Amazon Transcribe turns into real transcript segments. NB the sibling
# stereo-16k-30s.wav is synthetic tone bursts (won't transcribe) — do not use it
# here. We stream only the first LMA_TEST_STREAM_SECONDS of this ~5min clip.
_DEFAULT_WAV = (
    Path(__file__).resolve().parent.parent
    / "utilities/load-simulator/lma_load/fixtures/stereo-demo-call.wav"
)
_STREAM_SECONDS = float(os.environ.get("LMA_TEST_STREAM_SECONDS", "25"))


def _outputs(cfn, stack_name: str) -> dict:
    outs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def _event_sourcing_table(cfn, stack_name: str) -> str:
    resources = cfn.list_stack_resources(StackName=stack_name)["StackResourceSummaries"]
    ai_stack = next(
        (r["PhysicalResourceId"] for r in resources if r["LogicalResourceId"] == "AISTACK"),
        None,
    )
    if not ai_stack:
        raise RuntimeError("AISTACK nested stack not found")
    ai_resources = cfn.list_stack_resources(StackName=ai_stack)["StackResourceSummaries"]
    for r in ai_resources:
        if r["LogicalResourceId"] == "EventSourcingTable":
            return r["PhysicalResourceId"]
    raise RuntimeError("EventSourcingTable not found in AISTACK")


def _mint_tokens(region: str, pool_id: str, client_id: str, username: str, password: str) -> dict:
    """Mint Cognito tokens via SRP (the app client typically only allows SRP)."""
    from pycognito import Cognito

    u = Cognito(pool_id, client_id, username=username, user_pool_region=region)
    u.authenticate(password=password)
    return {"access": u.access_token, "id": u.id_token, "refresh": u.refresh_token}


async def _stream_wav(ws_url: str, tokens: dict, call_id: str, wav_path: Path,
                      agent_id: str, max_seconds: float = 35.0) -> str:
    """Stream a real WAV over the websocket at ~real-time pace. Returns final state."""
    import websockets

    wav = wave.open(str(wav_path), "rb")
    rate = wav.getframerate()
    channels = wav.getnchannels()
    sampwidth = wav.getsampwidth()
    chunk_ms = 200
    frames_per_chunk = int(rate * chunk_ms / 1000)

    url = (
        f"{ws_url}?authorization=Bearer%20{tokens['access']}"
        f"&id_token={tokens['id']}&refresh_token={tokens['refresh']}"
    )
    async with websockets.connect(url, max_size=None, open_timeout=25) as ws:
        # NB: do NOT send a `channels` field. The transcriber's CallMetaData
        # uses `channels` as an internal per-channel speaker map (channels['ch_0']
        # = {...}); the browser UI omits it and the transcriber initializes it.
        # Sending `channels: 2` (a count) makes processTranscriptionResults crash
        # with "Cannot create property 'ch_0' on number '2'" and produce zero
        # transcript segments. `channels` (the WAV channel count) is only used
        # locally below to pace/log; it is intentionally not in the START message.
        _ = channels
        await ws.send(json.dumps({
            "callEvent": "START",
            "callId": call_id,
            "fromNumber": "WS Probe",
            "toNumber": "LMA System",
            "agentId": agent_id,
            "samplingRate": rate,
        }))

        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            frames = wav.readframes(frames_per_chunk)
            if not frames:
                break
            await ws.send(frames)  # binary PCM
            # Pace slightly faster than real time (half the chunk duration). The
            # transcriber buffers audio in a BlockStream before forwarding to
            # Transcribe; strict real-time pacing over a short clip can leave the
            # last partial block un-flushed. Faster pacing keeps Transcribe fed
            # without overrunning it.
            await asyncio.sleep(chunk_ms / 2000.0)

        state = ws.state.name
        await ws.send(json.dumps({"callEvent": "END", "callId": call_id}))
        # brief grace so the END is flushed before we close
        await asyncio.sleep(1.0)
        return state


def _count_segments(ddb, table: str, call_id: str) -> int:
    resp = ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"trs#{call_id}"}},
        Select="COUNT",
    )
    return resp.get("Count", 0)


def _meeting_exists(ddb, table: str, call_id: str) -> bool:
    pk = f"c#{call_id}"
    return "Item" in ddb.get_item(TableName=table, Key={"PK": {"S": pk}, "SK": {"S": pk}})


def run_probe(stack_name: str, region: str | None = None,
              wav_path: str | None = None, verify_transcription: bool = True) -> dict:
    session = boto3.Session(region_name=region)
    region = session.region_name
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")
    outs = _outputs(cfn, stack_name)

    ws_ep = outs["LMAWebsocketEndpoint"]  # wss://<domain>/api/v1/ws
    client_id = outs["CognitoUserPoolClientId"]
    pool_id = outs.get("MCPServerUserPoolId") or ""
    if not pool_id:
        pool_id = outs.get("CognitoUserPoolTokenIssuerUrl", "").rsplit("/", 1)[-1]

    username = os.environ["LMA_TEST_USERNAME"]
    password = os.environ["LMA_TEST_PASSWORD"]
    tokens = _mint_tokens(region, pool_id, client_id, username, password)

    wav = Path(wav_path or os.environ.get("LMA_TEST_WAV") or _DEFAULT_WAV)
    if not wav.exists():
        raise FileNotFoundError(f"WAV fixture not found: {wav}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    call_id = f"ws-probe-{stamp}"
    state = asyncio.run(_stream_wav(ws_ep, tokens, call_id, wav, username,
                                    max_seconds=_STREAM_SECONDS))

    result = {
        "call_id": call_id, "ws_endpoint": ws_ep, "wav": str(wav),
        "final_state": state, "connected": state == "OPEN",
        "meeting_created": False, "segment_count": 0, "table": None,
    }
    if verify_transcription:
        table = _event_sourcing_table(cfn, stack_name)
        result["table"] = table
        # Poll: Transcribe + the KDS->Lambda pipeline take a few seconds after END.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if _meeting_exists(ddb, table, call_id):
                result["meeting_created"] = True
                result["segment_count"] = _count_segments(ddb, table, call_id)
                if result["segment_count"] > 0:
                    break
            time.sleep(3)
    return result


def cleanup(stack_name: str, call_id: str, region: str | None = None) -> None:
    """Delete the synthetic call's meeting + transcript-segment items."""
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")
    table = _event_sourcing_table(cfn, stack_name)
    for pk in (f"c#{call_id}", f"trs#{call_id}"):
        resp = ddb.query(
            TableName=table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": pk}},
        )
        for it in resp.get("Items", []):
            try:
                ddb.delete_item(TableName=table, Key={"PK": it["PK"], "SK": it["SK"]})
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    stack = sys.argv[1] if len(sys.argv) > 1 else "LMA"
    res = run_probe(stack)
    print(json.dumps(res, indent=2))
    ok = res["connected"] and res["meeting_created"] and res["segment_count"] > 0
    print("WS AUDIO E2E OK" if ok else "WS AUDIO E2E FAILED")
    if res["meeting_created"]:
        cleanup(stack, res["call_id"])
        print("cleaned up synthetic call")
    sys.exit(0 if ok else 1)
