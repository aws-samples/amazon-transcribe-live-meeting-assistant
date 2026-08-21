# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the endpointing rules.

The acceptance check is twofold: **an endpoint fires per the configured trailing
silence** and **firing promotes the current partial to a final**. The endpointer
is pure timing logic; no model weights,
no wheels — so these tests drive :class:`Endpointer` directly with chunk durations
and VAD speech/silence verdicts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from asr_server.endpointing import (
    EndpointDecision,
    Endpointer,
    EndpointingConfig,
    EndpointRule,
)

CHUNK_S = 0.1  # 100 ms chunks, the cadence the tests advance the endpointer at


# --- Helpers ----------------------------------------------------------------


def _feed(
    ep: Endpointer, count: int, *, is_speech: bool, duration_s: float = CHUNK_S
) -> list[EndpointDecision]:
    """Feed ``count`` identical chunks; return each chunk's decision."""
    return [ep.update(duration_s=duration_s, is_speech=is_speech) for _ in range(count)]


def _fired(decisions: list[EndpointDecision]) -> list[EndpointDecision]:
    return [d for d in decisions if d.is_endpoint]


# --- Core acceptance: endpoint fires per configured silence -----------------


def test_rule2_fires_after_configured_trailing_silence() -> None:
    """Primary acceptance: speech then the configured trailing silence endpoints."""
    ep = Endpointer(EndpointingConfig(rule2_min_trailing_silence_s=0.5))
    # 300 ms of speech, then silence. rule2 needs 500 ms of trailing silence.
    assert _fired(_feed(ep, 3, is_speech=True)) == []
    # 4 silent chunks = 400 ms: not yet.
    assert _fired(_feed(ep, 4, is_speech=False)) == []
    # 5th silent chunk crosses 500 ms -> fire.
    decision = ep.update(duration_s=CHUNK_S, is_speech=False)
    assert decision.is_endpoint
    assert decision.rule == 2
    assert decision.speech_final  # a genuine pause, not a forced cut
    assert decision.trailing_silence == pytest.approx(0.5)
    assert decision.utterance_length == pytest.approx(0.8)


def test_endpoint_promotes_only_once_then_resets() -> None:
    """Acceptance: an endpoint fires once, then the counters reset for the next
    utterance (partial->final promotion is a single edge, not a level)."""
    ep = Endpointer(EndpointingConfig(rule2_min_trailing_silence_s=0.2))
    _feed(ep, 2, is_speech=True)
    first = _fired(_feed(ep, 2, is_speech=False))
    assert len(first) == 1  # exactly one endpoint at the 200 ms mark
    # The fire auto-reset the counters, so the next utterance starts from zero.
    assert ep.utterance_length == 0.0
    assert ep.trailing_silence == 0.0
    # A fresh silence run must re-accumulate; the already-consumed silence doesn't
    # carry over, so a single new silent chunk does not immediately re-fire.
    assert not ep.update(duration_s=CHUNK_S, is_speech=False).is_endpoint


# --- rule1: trailing silence with no confident speech -----------------------


def test_rule1_fires_on_long_silence_without_speech() -> None:
    """rule1 flushes a gone-quiet stream even when it never contained speech."""
    ep = Endpointer(
        EndpointingConfig(
            rule1_min_trailing_silence_s=0.5,
            rule2_min_trailing_silence_s=1.2,
        )
    )
    # Pure silence from the start: rule2 can't fire (no nonsilence) but rule1 can.
    decisions = _feed(ep, 5, is_speech=False)
    fired = _fired(decisions)
    assert len(fired) == 1
    assert fired[0].rule == 1
    assert fired[0].speech_final
    assert fired[0].trailing_silence == pytest.approx(0.5)


def test_rule2_does_not_fire_without_nonsilence() -> None:
    """rule2 requires speech; pure silence shorter than rule1 fires nothing."""
    ep = Endpointer(
        EndpointingConfig(
            rule1_min_trailing_silence_s=10.0,  # far away
            rule2_min_trailing_silence_s=0.3,
        )
    )
    assert _fired(_feed(ep, 5, is_speech=False)) == []  # 500 ms silence, no speech


# --- rule3: max utterance length forces a cut -------------------------------


def test_rule3_forces_endpoint_regardless_of_silence() -> None:
    """rule3 caps a run-on utterance even while speech continues."""
    ep = Endpointer(EndpointingConfig(rule3_min_utterance_length_s=1.0))
    # Continuous speech (never any trailing silence) for 1.0 s -> forced cut.
    # 0.25 s chunks sum to exactly 1.0 (0.25 is float-exact; 0.1 would not be).
    decisions = _feed(ep, 4, is_speech=True, duration_s=0.25)
    fired = _fired(decisions)
    assert len(fired) == 1
    assert fired[0].rule == 3
    assert not fired[0].speech_final  # forced cut mid-speech, not a pause
    assert fired[0].utterance_length == pytest.approx(1.0)
    assert fired[0].trailing_silence == pytest.approx(0.0)


def test_rule3_cut_resets_for_continued_speech() -> None:
    """After a rule3 cut, ongoing speech accumulates into a fresh utterance."""
    ep = Endpointer(EndpointingConfig(rule3_min_utterance_length_s=0.5))
    # 1.0 s of speech in float-exact 0.25 s chunks = two 0.5 s caps.
    fired = _fired(_feed(ep, 4, is_speech=True, duration_s=0.25))
    assert [d.rule for d in fired] == [3, 3]


# --- Rule priority ----------------------------------------------------------


def test_speech_clears_trailing_silence_between_pauses() -> None:
    """A speech chunk resets the trailing-silence timer (a resumed utterance)."""
    ep = Endpointer(EndpointingConfig(rule2_min_trailing_silence_s=0.3))
    _feed(ep, 2, is_speech=True)
    _feed(ep, 2, is_speech=False)  # 200 ms silence, not yet 300 ms
    ep.update(duration_s=CHUNK_S, is_speech=True)  # speech resumes -> clear timer
    assert ep.trailing_silence == pytest.approx(0.0)
    # Now a fresh 300 ms silence run is required to fire.
    assert _fired(_feed(ep, 2, is_speech=False)) == []
    assert ep.update(duration_s=CHUNK_S, is_speech=False).is_endpoint


def test_rule1_reported_when_both_silence_rules_active() -> None:
    """When rule1 and rule2 are both satisfied, rule1 (priority order) is reported."""
    ep = Endpointer(
        EndpointingConfig(
            rule1_min_trailing_silence_s=0.3,
            rule2_min_trailing_silence_s=0.3,
        )
    )
    _feed(ep, 2, is_speech=True)
    fired = _fired(_feed(ep, 3, is_speech=False))
    assert len(fired) == 1
    assert fired[0].rule == 1  # first active rule in priority order wins


# --- Config mapping ---------------------------------------------------------


def test_from_endpointing_ms_maps_to_rule2() -> None:
    """The wire ``endpointing_ms`` maps onto rule2 and keeps the other defaults."""
    cfg = EndpointingConfig.from_endpointing_ms(800)
    assert cfg.rule2_min_trailing_silence_s == pytest.approx(0.8)
    assert cfg.rule1_min_trailing_silence_s == pytest.approx(2.4)  # default kept
    assert cfg.rule3_min_utterance_length_s == pytest.approx(20.0)  # default kept


def test_default_thresholds_match_recognizer_presets() -> None:
    """Defaults stay in lock-step with the sherpa preset in recognizer.py."""
    cfg = EndpointingConfig()
    assert cfg.rule1_min_trailing_silence_s == pytest.approx(2.4)
    assert cfg.rule2_min_trailing_silence_s == pytest.approx(1.2)
    assert cfg.rule3_min_utterance_length_s == pytest.approx(20.0)


def test_from_endpointing_ms_zero_is_allowed() -> None:
    cfg = EndpointingConfig.from_endpointing_ms(0)
    assert cfg.rule2_min_trailing_silence_s == 0.0


# --- Introspection ----------------------------------------------------------


def test_config_and_counters_exposed() -> None:
    cfg = EndpointingConfig(rule2_min_trailing_silence_s=0.4)
    ep = Endpointer(cfg)
    assert ep.config is cfg
    ep.update(duration_s=CHUNK_S, is_speech=True)
    ep.update(duration_s=CHUNK_S, is_speech=False)
    assert ep.utterance_length == pytest.approx(0.2)
    assert ep.trailing_silence == pytest.approx(0.1)


def test_reset_clears_counters() -> None:
    ep = Endpointer(EndpointingConfig(rule2_min_trailing_silence_s=5.0))
    _feed(ep, 3, is_speech=True)
    _feed(ep, 2, is_speech=False)
    ep.reset()
    assert ep.utterance_length == 0.0
    assert ep.trailing_silence == 0.0


def test_no_endpoint_decision_reports_current_counters() -> None:
    ep = Endpointer(EndpointingConfig(rule2_min_trailing_silence_s=5.0))
    decision = ep.update(duration_s=CHUNK_S, is_speech=True)
    assert not decision.is_endpoint
    assert decision.rule is None
    assert not decision.speech_final
    assert decision.utterance_length == pytest.approx(0.1)


def test_rule_is_active_predicate() -> None:
    rule = EndpointRule(
        must_contain_nonsilence=True,
        min_trailing_silence_s=0.5,
        min_utterance_length_s=0.0,
    )
    assert rule.is_active(trailing_silence_s=0.5, utterance_length_s=1.0, has_nonsilence=True)
    assert not rule.is_active(
        trailing_silence_s=0.5, utterance_length_s=1.0, has_nonsilence=False
    )
    assert not rule.is_active(
        trailing_silence_s=0.4, utterance_length_s=1.0, has_nonsilence=True
    )


# --- Frozen / immutability --------------------------------------------------


def test_decision_and_rule_are_frozen() -> None:
    decision = EndpointDecision(
        is_endpoint=False,
        rule=None,
        speech_final=False,
        utterance_length=0.0,
        trailing_silence=0.0,
    )
    rule = EndpointRule(
        must_contain_nonsilence=True, min_trailing_silence_s=1.0, min_utterance_length_s=0.0
    )
    with pytest.raises(AttributeError):
        decision.is_endpoint = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        rule.min_trailing_silence_s = 2.0  # type: ignore[misc]


# --- Constructor / input validation -----------------------------------------


def test_negative_endpointing_ms_rejected() -> None:
    with pytest.raises(ValueError, match="endpointing_ms"):
        EndpointingConfig.from_endpointing_ms(-1)


def test_negative_trailing_silence_rejected() -> None:
    with pytest.raises(ValueError, match="trailing-silence"):
        Endpointer(EndpointingConfig(rule2_min_trailing_silence_s=-0.1))


def test_negative_max_utterance_rejected() -> None:
    with pytest.raises(ValueError, match="rule3"):
        Endpointer(EndpointingConfig(rule3_min_utterance_length_s=-1.0))


def test_negative_duration_rejected() -> None:
    ep = Endpointer()
    with pytest.raises(ValueError, match="duration_s"):
        ep.update(duration_s=-0.1, is_speech=True)


def test_default_config_when_none() -> None:
    ep = Endpointer()
    assert ep.config.rule2_min_trailing_silence_s == pytest.approx(1.2)


# --- Fail-closed / dependency-free import contract --------------------------


def test_module_import_is_dependency_free() -> None:
    """Importing the module must not require ``onnxruntime`` or ``numpy``.

    Endpointing is pure stdlib timing logic; this proves the import stays inert so
    it can be imported inside the MicroVM/tooling before the ARM wheels exist.
    """
    code = (
        "import sys; "
        "sys.modules['onnxruntime'] = None; "
        "sys.modules['numpy'] = None; "
        "import asr_server.endpointing as e; "
        "assert hasattr(e, 'Endpointer'); "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
