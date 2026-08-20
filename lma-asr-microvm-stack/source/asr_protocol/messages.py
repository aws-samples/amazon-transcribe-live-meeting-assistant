"""Pydantic v2 models for every WebSocket wire message in ``design.md`` §5.

Both the ASR server (inside the MicroVM) and the control-plane router import
these models so the wire contract cannot drift. Each message carries a ``type``
discriminator; :data:`ClientMessage` and :data:`ServerMessage` are tagged
unions so an inbound frame can be parsed into the correct model by ``type``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, PositiveInt, TypeAdapter

# --- Client -> server ------------------------------------------------------


class Config(BaseModel):
    """Session config: query params AND/OR the optional first text frame (R2)."""

    type: Literal["config"] = "config"
    # "asr" streams transcripts. "embed" is a calibration mode: each binary frame
    # is one complete utterance and the reply is its speaker embedding, nothing
    # else. It exists because only this process has the embedding model, while the
    # statistics that turn embeddings into an operating point belong outside it.
    mode: Literal["asr", "embed"] = "asr"
    sample_rate: PositiveInt = 16000
    # Only 16-bit signed LE PCM is supported (R2.2); reject everything else.
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    # Only mono is supported for now (R2.2); a stereo stream would be silently
    # mis-decoded as mono, so reject channels != 1 rather than accept-and-garble.
    channels: Literal[1] = 1
    language: str = "en"
    interim_results: bool = True
    latency_mode: Literal["interactive", "balanced", "accurate"] = "balanced"
    endpointing_ms: int = 1200
    punctuate: bool = True
    word_timestamps: bool = True
    # Speaker diarization ("who spoke"). Requested per session, but only honoured
    # when the server was built with a speaker-embedding model ($ASR_DIARIZE); the
    # ``ready`` echo reports the EFFECTIVE value, so a client can tell whether it
    # actually got speaker labels rather than assuming it did.
    diarize: bool = False
    # Cap on distinct speakers (0 = discover as many as appear). Set it when the
    # conversation size is known, e.g. 2 for a two-party call.
    max_speakers: int = 0
    # Cosine similarity above which two utterances are treated as one speaker.
    # Higher splits more eagerly; lower merges more eagerly. The right value is
    # specific to the PAIRING of speaker model and utterance-length floor, so it is
    # negotiated per session rather than fixed at build time.
    #
    # ``None`` means "use the server's configured value", which is the operating
    # point calibrated for the model bundle the image was built with. It must NOT
    # default to a number: a concrete default would silently override that
    # calibrated value for every client that simply does not send the field.
    speaker_threshold: float | None = None
    # Shortest utterance worth embedding; a shorter one inherits the current
    # speaker instead of minting an identity from an unreliable embedding.
    # ``None`` means "use the server's configured value".
    min_segment_ms: int | None = None
    # Withhold the first embedding that matches nobody until a second one agrees
    # with it. ``None`` means "use the server's configured value".
    require_corroboration: bool | None = None
    # Split one endpointed utterance into a row per speaker turn, retroactively at
    # the end of the utterance.
    split_on_speaker_change: bool | None = None
    # Close a row as soon as a speaker change is confirmed, without waiting for
    # endpointing silence, so a speaker change rather than a pause is what separates
    # rows. ``None`` for all three means "use the server's configured value".
    live_turn_cut: bool | None = None
    turn_cut_interval_ms: int | None = None
    # Close a row after this much unbroken speech even with no boundary found, so a
    # monologue does not sit as one unlabelled row. 0 disables the bound.
    max_open_segment_ms: int | None = None


class Eos(BaseModel):
    """End-of-stream signal: flush, finalize, then ``termination`` (R6.3)."""

    type: Literal["eos"] = "eos"


# --- Server -> client ------------------------------------------------------


class Ready(BaseModel):
    """Handshake ack echoing the effective (defaults-applied) config (R2.3)."""

    type: Literal["ready"] = "ready"
    effective_config: Config
    session_id: str


class Partial(BaseModel):
    """Evolving interim transcript for a segment (R4.1).

    ``speaker`` is the **provisional** speaker for the in-progress segment (the
    last speaker identified), so a UI can attribute interim text immediately. The
    segment's audio is incomplete, so it is not embedded yet; the following
    ``final`` carries the authoritative label. ``None`` when diarization is off or
    no speaker has been identified yet.
    """

    type: Literal["partial"] = "partial"
    segment: int
    text: str
    start: float
    speaker: str | None = None


class Word(BaseModel):
    """Per-word timing carried on a ``final`` when ``word_timestamps`` is set."""

    w: str
    s: float
    e: float


class Final(BaseModel):
    """Finalized transcript for a segment at an utterance boundary (R4.2).

    ``speaker`` carries the diarized speaker label (e.g. ``"spk_0"``) when
    diarization is enabled, else ``None``. It is derived from the SAME audio
    samples and segment boundaries as this transcript, so the label cannot drift
    away from the text the way a separately-run diariser's turns would.
    """

    type: Literal["final"] = "final"
    segment: int
    text: str
    start: float
    end: float
    words: list[Word] = Field(default_factory=list)
    speaker: str | None = None


class Embedding(BaseModel):
    """One speaker embedding, in reply to a binary frame in ``embed`` mode.

    ``index`` counts frames in arrival order so a caller can match embeddings to
    the segments it sent without a request id.
    """

    type: Literal["embedding"] = "embedding"
    index: int
    dim: int
    vector: list[float]


class Termination(BaseModel):
    """End-of-session summary sent just before close (R6.3)."""

    type: Literal["termination"] = "termination"
    audio_seconds: float
    segments: int


class Error(BaseModel):
    """Structured error; ``fatal`` errors precede a close (design §10)."""

    type: Literal["error"] = "error"
    code: str
    message: str
    fatal: bool = True


class Warning(BaseModel):
    """Non-fatal advisory, e.g. the 8-hour-ceiling warning (design §10)."""

    type: Literal["warning"] = "warning"
    code: str
    message: str


# --- Tagged unions + parse helpers -----------------------------------------

ClientMessage = Annotated[Config | Eos, Field(discriminator="type")]
ServerMessage = Annotated[
    Ready | Partial | Final | Embedding | Termination | Error | Warning,
    Field(discriminator="type"),
]

_CLIENT_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_SERVER_ADAPTER: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(data: str | bytes) -> ClientMessage:
    """Parse a raw client->server text frame into its model by ``type``."""
    return _CLIENT_ADAPTER.validate_json(data)


def parse_server_message(data: str | bytes) -> ServerMessage:
    """Parse a raw server->client text frame into its model by ``type``."""
    return _SERVER_ADAPTER.validate_json(data)


__all__ = [
    "Config",
    "Eos",
    "Ready",
    "Partial",
    "Word",
    "Final",
    "Embedding",
    "Termination",
    "Error",
    "Warning",
    "ClientMessage",
    "ServerMessage",
    "parse_client_message",
    "parse_server_message",
]
