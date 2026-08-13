# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the build-time model fetch (no network).

This script runs inside `docker build`, so a mistake here is only visible minutes
into a deployment as a failed image build. Two things must hold: unverified
weights can never be laid down, and the archive's own file names must be
normalised to the names the runtime resolves by default, since that is what keeps
per-model environment variables out of the image.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import tarfile
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_model", Path(__file__).parents[1] / "scripts" / "fetch_model.py"
)
assert _SPEC is not None and _SPEC.loader is not None
fetch_model = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetch_model)

ENV_TEMPLATE = {
    "ASR_MODEL_ID": "test-model",
    "ASR_MODEL_URL": "https://example.invalid/model.tar.bz2",
    "ASR_MODEL_SHA256": "",  # filled in per test
    "ASR_MODEL_ARCHIVE": "tar.bz2",
    "ASR_MODEL_STRIP_COMPONENTS": "1",
    "ASR_MODEL_ENCODER_FILE": "encoder.int8.onnx",
    "ASR_MODEL_DECODER_FILE": "decoder.int8.onnx",
    "ASR_MODEL_JOINER_FILE": "joiner.int8.onnx",
    "ASR_MODEL_TOKENS_FILE": "tokens.txt",
}


def write_env(path: Path, values: dict[str, str]) -> Path:
    lines = ["# generated", ""]
    lines += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


def make_archive(tmp_path: Path, *, with_test_wav: bool = True) -> Path:
    """A tarball shaped like a real sherpa-onnx model release."""
    staging = tmp_path / "sherpa-onnx-test-model-int8"
    (staging / "test_wavs").mkdir(parents=True)
    (staging / "encoder.int8.onnx").write_bytes(b"encoder")
    (staging / "decoder.int8.onnx").write_bytes(b"decoder")
    (staging / "joiner.int8.onnx").write_bytes(b"joiner")
    # The fp32 twins a real release also ships; they must not be laid down.
    (staging / "encoder.onnx").write_bytes(b"fp32 encoder")
    (staging / "tokens.txt").write_text("a b c\n")
    (staging / "LICENSE").write_text("Example License\n")
    if with_test_wav:
        # 16 kHz mono 16-bit WAV header + a little silence.
        (staging / "test_wavs" / "0.wav").write_bytes(b"RIFF" + b"\x00" * 40)

    archive = tmp_path / "model.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(staging, arcname=staging.name)
    return archive


def test_read_env_file_parses_values_comments_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "model.env"
    path.write_text(
        '# a comment\n\nASR_MODEL_ID=abc\nASR_MODEL_LICENSE="Example License"\n'
        "ASR_SPEAKER_MODEL_URL=\nnot a pair\n"
    )

    values = fetch_model.read_env_file(path)

    assert values["ASR_MODEL_ID"] == "abc"
    assert values["ASR_MODEL_LICENSE"] == "Example License"
    assert values["ASR_SPEAKER_MODEL_URL"] == ""
    assert "not a pair" not in values


def test_fetch_verified_refuses_an_unpinned_checksum(tmp_path: Path) -> None:
    with pytest.raises(fetch_model.ModelFetchError, match="no SHA256 pinned"):
        fetch_model.fetch_verified(
            "https://example.invalid/x.tar.bz2", "", tmp_path / "out", "asr model"
        )


def test_fetch_verified_refuses_a_missing_url(tmp_path: Path) -> None:
    with pytest.raises(fetch_model.ModelFetchError, match="no URL configured"):
        fetch_model.fetch_verified("", "a" * 64, tmp_path / "out", "asr model")


def test_fetch_verified_refuses_a_mismatched_checksum(tmp_path: Path) -> None:
    target = tmp_path / "out"
    with mock.patch.object(fetch_model, "download", return_value="b" * 64), pytest.raises(
        fetch_model.ModelFetchError, match="SHA256 mismatch"
    ):
        fetch_model.fetch_verified(
            "https://example.invalid/x.tar.bz2", "a" * 64, target, "asr model"
        )


def test_a_transient_http_error_is_retried_rather_than_failing_the_build(tmp_path: Path) -> None:
    """A 503 from the release host used to cost a CloudFormation rollback.

    The download runs inside `docker build`, so one unlucky response failed the
    image, the nested stack, and the whole stack update — several minutes in.
    """
    attempts: list[int] = []
    delays: list[float] = []

    def flaky(url: str, target: Path) -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        return "a" * 64

    with mock.patch.object(fetch_model, "download", side_effect=flaky):
        digest = fetch_model.download_with_retries(
            "https://example.invalid/m.tar.bz2", tmp_path / "out", "asr model", sleep=delays.append
        )

    assert digest == "a" * 64
    assert len(attempts) == 3
    # Backing off rather than hammering a host that is already saying "not now".
    assert delays == [5.0, 10.0]


def test_a_404_is_not_retried_because_the_url_is_simply_wrong(tmp_path: Path) -> None:
    calls: list[int] = []

    def missing(url: str, target: Path) -> str:
        calls.append(1)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    with mock.patch.object(fetch_model, "download", side_effect=missing), pytest.raises(
        fetch_model.ModelFetchError, match="HTTP 404"
    ):
        fetch_model.download_with_retries(
            "https://example.invalid/m.tar.bz2", tmp_path / "out", "asr model", sleep=lambda _: None
        )

    assert len(calls) == 1


def test_a_truncated_download_is_retried_and_reported_if_it_never_completes(
    tmp_path: Path,
) -> None:
    # A cut connection raises IncompleteRead, which is not an OSError, so without
    # this it escaped as a traceback instead of a readable build failure.
    def truncated(url: str, target: Path) -> str:
        raise http.client.IncompleteRead(b"partial")

    with mock.patch.object(fetch_model, "download", side_effect=truncated), pytest.raises(
        fetch_model.ModelFetchError, match="download failed after 2 attempt"
    ):
        fetch_model.download_with_retries(
            "https://example.invalid/m.tar.bz2",
            tmp_path / "out",
            "asr model",
            attempts=2,
            sleep=lambda _: None,
        )


def test_run_lays_out_the_canonical_file_names(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    env_path = write_env(
        tmp_path / "model.env", {**ENV_TEMPLATE, "ASR_MODEL_SHA256": digest}
    )
    dest = tmp_path / "opt-models"

    def fake_download(url: str, target: Path) -> str:
        target.write_bytes(archive.read_bytes())
        return digest

    with mock.patch.object(fetch_model, "download", side_effect=fake_download):
        code = fetch_model.run(["--env-file", str(env_path), "--dest", str(dest)])

    assert code == 0
    # The runtime resolves <model dir>/encoder.onnx by default, so the int8
    # export has to arrive under that name and no ASR_MODEL_* path is needed.
    assert (dest / "encoder.onnx").read_bytes() == b"encoder"
    assert (dest / "decoder.onnx").read_bytes() == b"decoder"
    assert (dest / "joiner.onnx").read_bytes() == b"joiner"
    assert (dest / "tokens.txt").read_text() == "a b c\n"
    assert (dest / "LICENSE").read_text() == "Example License\n"
    assert (dest / "test_wavs" / "warm.wav").is_file()
    # Nothing left behind, and the unused fp32 twin was not kept.
    assert not (dest / "_staging").exists()
    assert not (dest / "encoder.int8.onnx").exists()


def test_run_downloads_the_speaker_model_when_one_is_selected(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    model_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    speaker_bytes = b"speaker weights"
    speaker_digest = hashlib.sha256(speaker_bytes).hexdigest()
    env_path = write_env(
        tmp_path / "model.env",
        {
            **ENV_TEMPLATE,
            "ASR_MODEL_SHA256": model_digest,
            "ASR_SPEAKER_MODEL_ID": "spk",
            "ASR_SPEAKER_MODEL_URL": "https://example.invalid/spk.onnx",
            "ASR_SPEAKER_MODEL_SHA256": speaker_digest,
        },
    )
    dest = tmp_path / "opt-models"

    def fake_download(url: str, target: Path) -> str:
        if url.endswith(".onnx"):
            target.write_bytes(speaker_bytes)
            return speaker_digest
        target.write_bytes(archive.read_bytes())
        return model_digest

    with mock.patch.object(fetch_model, "download", side_effect=fake_download):
        assert fetch_model.run(["--env-file", str(env_path), "--dest", str(dest)]) == 0

    # diarization.py resolves this exact filename by default.
    assert (dest / "speaker_embedding.onnx").read_bytes() == speaker_bytes


def test_run_builds_a_transcription_only_image_without_a_speaker_model(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    env_path = write_env(
        tmp_path / "model.env",
        {**ENV_TEMPLATE, "ASR_MODEL_SHA256": digest, "ASR_SPEAKER_MODEL_URL": ""},
    )
    dest = tmp_path / "opt-models"

    with mock.patch.object(
        fetch_model,
        "download",
        side_effect=lambda url, target: (target.write_bytes(archive.read_bytes()), digest)[1],
    ):
        assert fetch_model.run(["--env-file", str(env_path), "--dest", str(dest)]) == 0

    assert not (dest / "speaker_embedding.onnx").exists()


def test_run_fails_when_a_named_file_is_not_in_the_archive(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    env_path = write_env(
        tmp_path / "model.env",
        {**ENV_TEMPLATE, "ASR_MODEL_SHA256": digest, "ASR_MODEL_ENCODER_FILE": "nope.onnx"},
    )

    with mock.patch.object(
        fetch_model,
        "download",
        side_effect=lambda url, target: (target.write_bytes(archive.read_bytes()), digest)[1],
    ):
        code = fetch_model.run(["--env-file", str(env_path), "--dest", str(tmp_path / "d")])

    assert code == 1


def test_run_fails_on_an_incomplete_env_file(tmp_path: Path) -> None:
    env_path = write_env(tmp_path / "model.env", {"ASR_MODEL_URL": "https://example.invalid/x"})

    assert fetch_model.run(["--env-file", str(env_path), "--dest", str(tmp_path / "d")]) == 1


def test_run_fails_on_a_missing_env_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "absent.env")
    assert fetch_model.run(["--env-file", missing, "--dest", str(tmp_path)]) == 1


def test_extract_rejects_an_unsupported_archive_type(tmp_path: Path) -> None:
    with pytest.raises(fetch_model.ModelFetchError, match="unsupported archive type"):
        fetch_model.extract(tmp_path / "x", "zip", 1, tmp_path)
