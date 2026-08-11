"""Round-trip serialization tests for every wire message.

Covers each message in ``design.md`` §5, plus the tagged-union parse helpers
that both the router and the ASR server use to decode inbound text frames.
"""

from __future__ import annotations

import pytest
from asr_protocol import (
    Config,
    Eos,
    Error,
    Final,
    Partial,
    Ready,
    Termination,
    Warning,
    Word,
    parse_client_message,
    parse_server_message,
)
from pydantic import BaseModel, ValidationError

# One representative, fully-populated instance of each message type.
MESSAGES: list[BaseModel] = [
    Config(),
    Config(
        sample_rate=8000,
        encoding="pcm_s16le",
        channels=1,
        language="es",
        interim_results=False,
        latency_mode="accurate",
        endpointing_ms=800,
        punctuate=False,
        word_timestamps=False,
    ),
    Eos(),
    Ready(effective_config=Config(), session_id="sess-abc123"),
    Partial(segment=3, text="let's meet at noon", start=12.44),
    Final(
        segment=3,
        text="Let's meet at noon.",
        start=12.44,
        end=13.98,
        words=[
            Word(w="Let's", s=12.44, e=12.70),
            Word(w="meet", s=12.70, e=12.98),
        ],
    ),
    Termination(audio_seconds=74.6, segments=18),
    Error(code="BAD_ENCODING", message="unsupported encoding", fatal=True),
    Warning(code="SESSION_CEILING", message="8-hour ceiling approaching"),
]


@pytest.mark.parametrize("msg", MESSAGES, ids=lambda m: type(m).__name__)
def test_round_trip_json(msg: BaseModel) -> None:
    """Every message survives a serialize -> deserialize round trip intact."""
    restored = type(msg).model_validate_json(msg.model_dump_json())
    assert restored == msg


@pytest.mark.parametrize("msg", MESSAGES, ids=lambda m: type(m).__name__)
def test_round_trip_dict(msg: BaseModel) -> None:
    """Dict round trip preserves equality as well."""
    restored = type(msg).model_validate(msg.model_dump())
    assert restored == msg


def test_config_defaults_match_design() -> None:
    """Config defaults match the schema in design.md §5.1."""
    cfg = Config()
    assert cfg.type == "config"
    assert cfg.sample_rate == 16000
    assert cfg.encoding == "pcm_s16le"
    assert cfg.channels == 1
    assert cfg.language == "en"
    assert cfg.interim_results is True
    assert cfg.latency_mode == "balanced"
    assert cfg.endpointing_ms == 1200
    assert cfg.punctuate is True
    assert cfg.word_timestamps is True


@pytest.mark.parametrize("bad_rate", [0, -1, -16000])
def test_config_rejects_non_positive_sample_rate(bad_rate: int) -> None:
    """sample_rate must be positive (R2.2)."""
    with pytest.raises(ValidationError):
        Config(sample_rate=bad_rate)


@pytest.mark.parametrize("bad_encoding", ["mp3", "pcm_f32le", "opus", ""])
def test_config_rejects_unsupported_encoding(bad_encoding: str) -> None:
    """Only pcm_s16le is supported; anything else is rejected (R2.2)."""
    with pytest.raises(ValidationError):
        # intentionally invalid to assert runtime ValidationError
        Config(encoding=bad_encoding)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_channels", [2, 0, -1])
def test_config_rejects_non_mono_channels(bad_channels: int) -> None:
    """Only mono (channels=1) is supported; stereo etc. is rejected (R2.2).

    A non-mono stream would otherwise be silently mis-decoded as mono, so the
    wire model rejects it up front rather than accept-and-garble.
    """
    with pytest.raises(ValidationError):
        # intentionally invalid to assert runtime ValidationError
        Config(channels=bad_channels)  # type: ignore[arg-type]


def test_config_accepts_mono_channels() -> None:
    """channels=1 is valid and is the applied default (R2.2)."""
    assert Config(channels=1).channels == 1
    assert Config().channels == 1


def test_final_words_default_empty() -> None:
    """words defaults to an empty list and instances do not share it."""
    a = Final(segment=1, text="a", start=0.0, end=1.0)
    b = Final(segment=2, text="b", start=1.0, end=2.0)
    assert a.words == []
    a.words.append(Word(w="x", s=0.0, e=0.1))
    assert b.words == []


def test_parse_client_message_dispatches_by_type() -> None:
    """The client union parses each type into the right model."""
    assert isinstance(parse_client_message(Config().model_dump_json()), Config)
    assert isinstance(parse_client_message(Eos().model_dump_json()), Eos)


def test_parse_server_message_dispatches_by_type() -> None:
    """The server union parses each type into the right model."""
    cases: list[BaseModel] = [
        Ready(effective_config=Config(), session_id="s"),
        Partial(segment=0, text="", start=0.0),
        Final(segment=0, text="", start=0.0, end=0.0),
        Termination(audio_seconds=0.0, segments=0),
        Error(code="X", message="y"),
        Warning(code="X", message="y"),
    ]
    for msg in cases:
        parsed = parse_server_message(msg.model_dump_json())
        assert type(parsed) is type(msg)
        assert parsed == msg


def test_parse_rejects_unknown_type() -> None:
    """An unknown discriminator is rejected rather than silently accepted."""
    with pytest.raises(ValidationError):
        parse_client_message('{"type": "nope"}')
