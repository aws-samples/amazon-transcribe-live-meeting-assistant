# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Config-driven trailing-silence and max-utterance endpointing rules.

Decides when a partial hypothesis is promoted to a FINAL, i.e. when an utterance
boundary is reached. This is **pure timing logic**: no
ML, no heavy deps — a small config-driven state machine that mirrors sherpa-onnx's
three-rule ``OnlineEndpointConfig`` so the streaming and VAD-segmented paths can
share one endpointing contract.

The three rules are config-driven:

* **rule1** — fire after ``rule1_min_trailing_silence_s`` (e.g. 2.4 s) of trailing
  silence *regardless of whether any speech was seen*. Flushes a stream that has
  gone quiet even when nothing was confidently spoken (a lone word, a false start).
* **rule2** — fire after ``rule2_min_trailing_silence_s`` (e.g. 1.2 s) of trailing
  silence *once the utterance contains speech*. The primary "they stopped talking"
  rule, driven by the wire ``Config.endpointing_ms`` field
  (see :meth:`EndpointingConfig.from_endpointing_ms`).
* **rule3** — fire once the utterance reaches ``rule3_min_utterance_length_s``
  (e.g. 20 s) *regardless of silence*, forcing a cut so a run-on segment can't grow
  unbounded.

Like :mod:`asr_server.vad` and :mod:`asr_server.recognizer` these are
**engine-internal** types: this module never imports the ``asr_protocol`` wire
schema, so the two can evolve independently. It is pure stdlib and pulls in no
inference wheels, so it is always importable inside the MicroVM and under tooling.

"nonsilence" here is *acoustic* speech as reported by the VAD gate
(:class:`asr_server.vad.VadGate`), not decoded text — matching sherpa's model.
Drive :meth:`Endpointer.update` once per audio chunk with the chunk duration and
the gate's speech/silence verdict; a fired :class:`EndpointDecision` tells the
caller to finalize the current segment (and reset the recogniser) — the
``speech_final`` flag distinguishes a genuine pause from a forced length cut.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "EndpointRule",
    "EndpointingConfig",
    "EndpointDecision",
    "Endpointer",
]

# rule3 is the max-utterance-length rule; a fire on it is a forced cut mid-speech
# rather than a detected pause, so it is reported with ``speech_final=False``.
_MAX_UTTERANCE_RULE = 3


# --- Rules ------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointRule:
    """One sherpa-style endpointing rule (engine-internal, not the wire model).

    The rule fires when the utterance is at least ``min_utterance_length_s`` long,
    the trailing silence is at least ``min_trailing_silence_s``, and — only if
    ``must_contain_nonsilence`` is set — the utterance actually contains speech.
    """

    must_contain_nonsilence: bool
    min_trailing_silence_s: float
    min_utterance_length_s: float

    def is_active(
        self,
        *,
        trailing_silence_s: float,
        utterance_length_s: float,
        has_nonsilence: bool,
    ) -> bool:
        """Whether this rule fires for the given utterance state."""
        return (
            (has_nonsilence or not self.must_contain_nonsilence)
            and trailing_silence_s >= self.min_trailing_silence_s
            and utterance_length_s >= self.min_utterance_length_s
        )


@dataclass(frozen=True)
class EndpointingConfig:
    """Config-driven thresholds for the three endpointing rules.

    Defaults match the sherpa-onnx streaming presets recorded in
    :class:`asr_server.recognizer.SherpaModelConfig`, kept in lock-step so the
    explicit endpointer and the backend's built-in detector agree.
    """

    rule1_min_trailing_silence_s: float = 2.4
    rule2_min_trailing_silence_s: float = 1.2
    rule3_min_utterance_length_s: float = 20.0

    @classmethod
    def from_endpointing_ms(cls, endpointing_ms: int) -> EndpointingConfig:
        """Build a config whose primary (rule2) silence is ``endpointing_ms``.

        Maps the wire ``Config.endpointing_ms`` field (design §5.1) onto rule2 —
        the "they stopped talking" threshold — keeping the rule1/rule3 defaults.
        Taken as a plain int so this module stays free of the ``asr_protocol``
        wire schema.
        """
        if endpointing_ms < 0:
            raise ValueError("endpointing_ms must be >= 0")
        return cls(rule2_min_trailing_silence_s=endpointing_ms / 1000.0)

    def rules(self) -> tuple[EndpointRule, EndpointRule, EndpointRule]:
        """The three rules in priority order (rule1, rule2, rule3)."""
        return (
            EndpointRule(
                must_contain_nonsilence=False,
                min_trailing_silence_s=self.rule1_min_trailing_silence_s,
                min_utterance_length_s=0.0,
            ),
            EndpointRule(
                must_contain_nonsilence=True,
                min_trailing_silence_s=self.rule2_min_trailing_silence_s,
                min_utterance_length_s=0.0,
            ),
            EndpointRule(
                must_contain_nonsilence=False,
                min_trailing_silence_s=0.0,
                min_utterance_length_s=self.rule3_min_utterance_length_s,
            ),
        )


# --- Decision ---------------------------------------------------------------


@dataclass(frozen=True)
class EndpointDecision:
    """Outcome of one :meth:`Endpointer.update` (engine-internal, not the wire model).

    ``is_endpoint`` is ``True`` when an utterance boundary is reached and the
    current partial should be promoted to a ``final``. ``rule`` is the 1-based
    index of the rule that fired (``None`` when no endpoint). ``speech_final`` is
    ``True`` when the boundary was reached via trailing silence (rules 1 & 2 — a
    genuine pause) and ``False`` for a forced max-utterance cut (rule 3), which
    slices mid-speech. ``utterance_length`` / ``trailing_silence`` are the audio
    seconds observed at the decision instant (before the counters reset on a fire).
    """

    is_endpoint: bool
    rule: int | None
    speech_final: bool
    utterance_length: float
    trailing_silence: float


# --- Endpointer -------------------------------------------------------------


class Endpointer:
    """Config-driven trailing-silence / max-utterance endpointing state machine.

    Fed one audio chunk at a time via :meth:`update` with the chunk duration and
    the VAD gate's speech/silence verdict. Tracks the running utterance length and
    the contiguous trailing silence; when any configured rule activates it reports
    an endpoint and auto-resets so the next chunk begins a fresh utterance. A
    silence update that satisfies no rule simply accumulates; a speech update
    clears the trailing-silence timer (dip hysteresis lives upstream in the VAD
    gate, so a chunk flagged silence here is already a confirmed pause).
    """

    def __init__(self, config: EndpointingConfig | None = None) -> None:
        cfg = config if config is not None else EndpointingConfig()
        if cfg.rule1_min_trailing_silence_s < 0 or cfg.rule2_min_trailing_silence_s < 0:
            raise ValueError("rule trailing-silence thresholds must be >= 0")
        if cfg.rule3_min_utterance_length_s < 0:
            raise ValueError("rule3_min_utterance_length_s must be >= 0")
        self._config = cfg
        self._rules = cfg.rules()
        self._utterance_length = 0.0
        self._trailing_silence = 0.0

    @property
    def config(self) -> EndpointingConfig:
        """The resolved thresholds this endpointer applies."""
        return self._config

    @property
    def utterance_length(self) -> float:
        """Audio seconds accumulated in the in-progress utterance."""
        return self._utterance_length

    @property
    def trailing_silence(self) -> float:
        """Contiguous trailing-silence seconds at the tail of the utterance."""
        return self._trailing_silence

    def update(self, *, duration_s: float, is_speech: bool) -> EndpointDecision:
        """Advance by one chunk; report whether an utterance boundary is reached.

        ``duration_s`` is the chunk's audio length in seconds; ``is_speech`` is the
        VAD verdict for it. On an endpoint the counters auto-reset (the returned
        decision still reports the length/silence observed at the firing instant).
        """
        if duration_s < 0:
            raise ValueError("duration_s must be >= 0")
        self._utterance_length += duration_s
        if is_speech:
            self._trailing_silence = 0.0
        else:
            self._trailing_silence += duration_s

        # sherpa's definition: the utterance "contains speech" iff some of it is
        # not part of the current trailing-silence run.
        has_nonsilence = self._utterance_length > self._trailing_silence

        for index, rule in enumerate(self._rules, start=1):
            if rule.is_active(
                trailing_silence_s=self._trailing_silence,
                utterance_length_s=self._utterance_length,
                has_nonsilence=has_nonsilence,
            ):
                decision = EndpointDecision(
                    is_endpoint=True,
                    rule=index,
                    speech_final=index != _MAX_UTTERANCE_RULE,
                    utterance_length=self._utterance_length,
                    trailing_silence=self._trailing_silence,
                )
                self.reset()
                return decision

        return EndpointDecision(
            is_endpoint=False,
            rule=None,
            speech_final=False,
            utterance_length=self._utterance_length,
            trailing_silence=self._trailing_silence,
        )

    def reset(self) -> None:
        """Clear the utterance/silence counters to begin a fresh utterance."""
        self._utterance_length = 0.0
        self._trailing_silence = 0.0
