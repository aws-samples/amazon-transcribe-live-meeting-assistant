# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""End-to-end WebSocket streaming probe (the transcriber front door).

Connects to the live transcriber WebSocket exactly like the browser Stream Audio
tab does — mints a real Cognito access token (SRP), opens wss://.../api/v1/ws,
sends a START control message, streams a few synthetic stereo PCM chunks, and
asserts the connection stays OPEN (i.e. the @fastify/websocket upgrade was
accepted). This is the regression guard for the fastify 5 / @fastify/websocket
11 route-registration-ordering bug, which returned HTTP 500 "ws.on is not a
function" on every connect and made the browser reconnect-loop without ever
sending START (so no meeting was created).

Requires a Cognito user's credentials, provided via env:
    LMA_TEST_USERNAME, LMA_TEST_PASSWORD
(and optionally LMA_TEST_MEETING_ASSERT=1 to also poll DynamoDB for the meeting).

Depends on `pycognito` and `websockets` (installed in the repo venv for integ
testing). If either is missing, the test that uses this probe skips.

Standalone:
    LMA_TEST_USERNAME=you@x LMA_TEST_PASSWORD=... \
      AWS_PROFILE=default python integ-tests/ws_stream_probe.py lma-integtest1
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
from datetime import datetime, timezone

import boto3


def _outputs(cfn, stack_name: str) -> dict:
    outs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def _mint_tokens(region: str, pool_id: str, client_id: str, username: str, password: str) -> dict:
    """Mint Cognito tokens via SRP (the app client typically only allows SRP)."""
    from pycognito import Cognito

    u = Cognito(pool_id, client_id, username=username, user_pool_region=region)
    u.authenticate(password=password)
    return {"access": u.access_token, "id": u.id_token, "refresh": u.refresh_token}


def _stereo_pcm_chunk(ms: int = 200, rate: int = 8000) -> bytes:
    n = int(rate * ms / 1000)
    buf = bytearray()
    for i in range(n):
        s = int(3000 * math.sin(2 * math.pi * 440 * i / rate))
        buf += struct.pack("<h", s)  # channel 0
        buf += struct.pack("<h", s)  # channel 1
    return bytes(buf)


async def _stream(ws_url: str, tokens: dict, call_id: str, seconds: int = 3) -> str:
    import websockets

    url = (
        f"{ws_url}?authorization=Bearer%20{tokens['access']}"
        f"&id_token={tokens['id']}&refresh_token={tokens['refresh']}"
    )
    async with websockets.connect(url, max_size=None, open_timeout=25) as ws:
        await ws.send(json.dumps({
            "callEvent": "START",
            "callId": call_id,
            "fromNumber": "WS Probe",
            "toNumber": "LMA System",
            "agentId": os.environ.get("LMA_TEST_USERNAME", "ws-probe@lma.aws"),
            "samplingRate": 8000,
            "channels": 2,
        }))
        for _ in range(seconds * 5):
            await ws.send(_stereo_pcm_chunk())
            await asyncio.sleep(0.2)
        state = ws.state.name
        await ws.send(json.dumps({"callEvent": "END", "callId": call_id}))
        return state  # "OPEN" means the upgrade held the whole session


def run_probe(stack_name: str, region: str | None = None) -> dict:
    session = boto3.Session(region_name=region)
    region = session.region_name
    cfn = session.client("cloudformation")
    outs = _outputs(cfn, stack_name)

    ws_ep = outs["LMAWebsocketEndpoint"]  # wss://<domain>/api/v1/ws
    pool_id = outs.get("MCPServerUserPoolId") or ""
    client_id = outs["CognitoUserPoolClientId"]
    # Derive pool id from the issuer url if not directly present.
    if not pool_id:
        issuer = outs.get("CognitoUserPoolTokenIssuerUrl", "")
        pool_id = issuer.rsplit("/", 1)[-1]

    username = os.environ["LMA_TEST_USERNAME"]
    password = os.environ["LMA_TEST_PASSWORD"]
    tokens = _mint_tokens(region, pool_id, client_id, username, password)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    call_id = f"ws-probe-{stamp}"
    state = asyncio.run(_stream(ws_ep, tokens, call_id))

    return {"call_id": call_id, "ws_endpoint": ws_ep, "final_state": state,
            "connected": state == "OPEN"}


if __name__ == "__main__":
    stack = sys.argv[1] if len(sys.argv) > 1 else "LMA"
    result = run_probe(stack)
    print(json.dumps(result, indent=2))
    print("WS STREAM OK" if result["connected"] else "WS STREAM FAILED")
    sys.exit(0 if result["connected"] else 1)
