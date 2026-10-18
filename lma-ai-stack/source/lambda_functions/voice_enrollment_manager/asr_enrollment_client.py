# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Turn enrollment recordings into speaker embeddings, using the ASR runtime.

The embedding model lives in the ASR MicroVM image, not in this function, and
the ASR protocol already has an ``embed`` mode: send a config with
``mode="embed"``, then one binary frame per utterance, and the server replies
with one ``embedding`` message per frame. So this carries no model and no
inference dependency -- it opens a short-lived session and reads vectors back.

**The audio is never written anywhere.** It arrives in the request, is held in
memory for the length of this call, and is discarded when the call returns. What
persists is the centroid computed from the vectors, which is a one-way summary:
enough to recognise the voice, not enough to reconstruct the speech.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

LOGGER = logging.getLogger()

# Where to reach an ASR session for embedding. In a deployment this is the
# MicroVM launcher's endpoint; ASR_DIRECT_ENDPOINT bypasses it for local work,
# the same escape hatch the transcriber uses.
ASR_EMBED_ENDPOINT = os.environ.get("ASR_EMBED_ENDPOINT", "")
ASR_DIRECT_ENDPOINT = os.environ.get("ASR_DIRECT_ENDPOINT", "")
ASR_SAMPLE_RATE = int(os.environ.get("ASR_SAMPLE_RATE", "16000"))

# A whole enrollment is half a minute of speech in a handful of clips, so this
# is generous. It exists so a session that stops replying cannot hold the
# function open to its timeout.
EMBED_TIMEOUT_SECONDS = float(os.environ.get("ASR_EMBED_TIMEOUT_SECONDS", "45"))

# Guards against a request that would cost a lot of work: enrollment needs a few
# clips of a few seconds, and anything far beyond that is not an enrollment.
MAX_UTTERANCES = 12
MAX_UTTERANCE_BYTES = 2 * ASR_SAMPLE_RATE * 30  # 16-bit mono, 30 seconds


def _decode(samples):
    """Base64 PCM from the browser into raw 16-bit frames, with bounds."""
    if len(samples) > MAX_UTTERANCES:
        raise ValueError(f"at most {MAX_UTTERANCES} recordings")
    frames = []
    for encoded in samples:
        if not isinstance(encoded, str):
            raise ValueError("each recording must be base64 PCM")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("a recording was not valid base64") from error
        if not raw or len(raw) % 2:
            raise ValueError("a recording was not 16-bit PCM")
        if len(raw) > MAX_UTTERANCE_BYTES:
            raise ValueError("a recording was too long")
        frames.append(raw)
    if not frames:
        raise ValueError("no recordings to embed")
    return frames


async def _embed(frames, endpoint):
    """One ``embed``-mode session: config, frames, then read the vectors back."""
    import websockets  # noqa: PLC0415 - optional at import time, as elsewhere

    vectors = []
    async with websockets.connect(endpoint, max_size=None, open_timeout=20) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "config",
                    "mode": "embed",
                    "sample_rate": ASR_SAMPLE_RATE,
                    "encoding": "pcm_s16le",
                    "channels": 1,
                }
            )
        )
        for frame in frames:
            await socket.send(frame)
        # `eos` tells the server no more audio is coming, so it finishes and
        # closes rather than waiting on a socket that will never speak again.
        await socket.send(json.dumps({"type": "eos"}))

        while len(vectors) < len(frames):
            message = await socket.recv()
            if isinstance(message, bytes):
                continue
            payload = json.loads(message)
            kind = payload.get("type")
            if kind == "embedding":
                vectors.append([float(value) for value in payload.get("vector") or []])
            elif kind == "error":
                raise RuntimeError(f"embedding failed: {payload.get('message')}")
            elif kind == "termination":
                break
    return vectors


def embed_utterances(samples):
    """Embeddings for a list of base64 PCM recordings.

    Raises ``ValueError`` for a request that is not a plausible enrollment and
    ``RuntimeError`` when the ASR session cannot produce vectors -- the caller
    turns both into "could not use those recordings" rather than surfacing
    either to the browser.
    """
    endpoint = ASR_DIRECT_ENDPOINT or ASR_EMBED_ENDPOINT
    if not endpoint:
        raise RuntimeError("no ASR endpoint is configured for embedding")

    frames = _decode(samples)
    try:
        vectors = asyncio.run(
            asyncio.wait_for(_embed(frames, endpoint), timeout=EMBED_TIMEOUT_SECONDS)
        )
    except TimeoutError as error:
        raise RuntimeError("the embedding session did not respond in time") from error

    usable = [vector for vector in vectors if vector]
    if not usable:
        raise RuntimeError("the embedding session returned no vectors")
    LOGGER.info("Embedded %d of %d enrollment recordings", len(usable), len(frames))
    return usable
