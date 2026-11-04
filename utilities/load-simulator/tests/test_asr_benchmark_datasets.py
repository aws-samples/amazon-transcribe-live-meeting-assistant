# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Packaged ground-truth meetings for the ASR benchmark."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from lma_load.asr_benchmark import datasets, quality
from lma_load.asr_benchmark.datasets import ami
from lma_load.cli import load_command

WORDS = (
    '<w nite:id="x1" starttime="1.0" endtime="1.4">Hello</w>'
    '<w nite:id="x2" starttime="1.4" endtime="1.4" punc="true">,</w>'
    '<vocalsound nite:id="x3" starttime="1.5" endtime="1.9" type="laugh"/>'
    '<w nite:id="x4" starttime="1.5" endtime="1.9">there</w>'
    '<w nite:id="x5" starttime="4.0" endtime="4.3">Okay</w>'
    '<w nite:id="x6" starttime="4.3" endtime="4.3" punc="true">.</w>'
)


def test_words_keep_their_punctuation_and_skip_other_annotations() -> None:
    assert ami.read_words(WORDS) == [(1.0, 1.4, "Hello,"), (1.5, 1.9, "there"), (4.0, 4.3, "Okay.")]


def test_character_entities_in_the_annotations_are_decoded() -> None:
    xml = '<w nite:id="x1" starttime="1.0" endtime="1.4">couldn&#39;t</w>'
    assert ami.read_words(xml) == [(1.0, 1.4, "couldn't")]


def test_words_are_joined_into_utterances_until_a_pause() -> None:
    assert ami.utterances(ami.read_words(WORDS)) == [
        (1.0, 1.9, "Hello, there"),
        (4.0, 4.3, "Okay."),
    ]


def test_the_host_is_on_the_microphone_channel_and_everyone_else_on_the_meeting_channel() -> None:
    stm = ami.build_stm("M1", "B", {"A": "0", "B": "1"}, {"A": WORDS, "B": WORDS})
    reference = quality.parse_stm(stm)
    assert reference.words("AGENT") == reference.words("CALLER") == ["hello", "there", "okay"]
    assert {s.speaker for s in reference.segments if s.channel == "AGENT"} == {"M1_B"}


def _write(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(ami.SAMPLE_RATE)
        wf.writeframes(samples.astype("<i2").tobytes())
    return path


def test_each_headset_is_kept_only_around_its_wearers_speech(tmp_path: Path) -> None:
    second = ami.SAMPLE_RATE
    tone = np.full(4 * second, 1000)
    headsets = {"A": _write(tmp_path / "a.wav", tone), "B": _write(tmp_path / "b.wav", tone)}
    stm = "M1 0 M1_A 0.50 1.00 hi\nM1 1 M1_B 2.50 3.00 hello\n"
    out = ami.mix(headsets, "B", stm, tmp_path / "out.wav")
    with wave.open(str(out), "rb") as wf:
        assert (wf.getnchannels(), wf.getframerate()) == (2, ami.SAMPLE_RATE)
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").reshape(-1, 2)
    meeting, mic = pcm[:, 0], pcm[:, 1]
    assert meeting[int(0.75 * second)] != 0 and meeting[int(2.75 * second)] == 0
    assert mic[int(2.75 * second)] != 0 and mic[int(0.75 * second)] == 0
    assert meeting[int(0.25 * second)] != 0 and meeting[int(0.15 * second)] == 0


def test_mixing_is_byte_for_byte_repeatable(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    headsets = {k: _write(tmp_path / f"{k}.wav", rng.integers(-3000, 3000, 32000)) for k in "AB"}
    stm = "M1 0 M1_A 0.10 1.50 hi\nM1 1 M1_B 0.20 1.80 hello\n"
    first = ami.sha256(ami.mix(headsets, "B", stm, tmp_path / "one.wav"))
    assert first == ami.sha256(ami.mix(headsets, "B", stm, tmp_path / "two.wav"))


def test_downloads_come_only_from_the_ami_mirror(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing"):
        ami._download("https://example.com/x.wav", tmp_path / "x.wav", "0" * 64)


def test_every_packaged_meeting_has_a_reference_with_both_channels() -> None:
    manifest = ami.manifest()
    assert manifest["licence"] == "CC BY 4.0"
    assert set(manifest["meetings"]) >= {"IS1009c", "ES2004a"}
    for meeting, entry in manifest["meetings"].items():
        reference = quality.load_reference(ami.reference_path(meeting))
        assert reference.by_channel
        assert len(reference.words("CALLER")) > 1000 and len(reference.words("AGENT")) > 200
        assert reference.speakers("AGENT") == 1
        assert reference.speakers("CALLER") == len(entry["speakers"]) - 1
        assert all(len(s["sha256"]) == 64 for s in entry["speakers"].values())


def test_an_unknown_dataset_is_refused_with_the_choices() -> None:
    with pytest.raises(ValueError, match="ami/IS1009c"):
        datasets.prepare("ami/NOPE")
    assert "ami/ES2004a" in datasets.available()


def test_the_cli_takes_a_wav_or_a_dataset_but_not_both(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    runner = CliRunner()
    both = runner.invoke(
        load_command,
        [
            "asr-benchmark",
            "-s",
            "dev",
            "--region",
            "us-east-1",
            "--transcribe",
            "--wav",
            str(wav),
            "--dataset",
            "ami/IS1009c",
        ],
    )
    neither = runner.invoke(
        load_command, ["asr-benchmark", "-s", "dev", "--region", "us-east-1", "--transcribe"]
    )
    assert "exactly one of --wav and --dataset" in both.output
    assert "exactly one of --wav and --dataset" in neither.output
