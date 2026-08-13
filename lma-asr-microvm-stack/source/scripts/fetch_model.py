# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Download, verify and normalise the ASR model weights during the image build.

Reads the ``model.env`` written by the AsrImageSource custom resource (or the
committed default) and lays the selected model out under ``--dest`` using the
canonical filenames ``asr_server.recognizer.build_model_config`` resolves by
default, so no per-model environment variables have to reach the runtime.

Fails closed: a missing URL, a missing or mismatched SHA256, or a missing model
file after extraction aborts the build.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CANONICAL_NAMES = {
    "ASR_MODEL_ENCODER_FILE": "encoder.onnx",
    "ASR_MODEL_DECODER_FILE": "decoder.onnx",
    "ASR_MODEL_JOINER_FILE": "joiner.onnx",
    "ASR_MODEL_TOKENS_FILE": "tokens.txt",
}

SPEAKER_MODEL_NAME = "speaker_embedding.onnx"
SEGMENTATION_MODEL_NAME = "segmentation.onnx"
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# Model weights come from a public release host, and a single 503 from it used to
# fail the whole image build — which surfaces as a CloudFormation rollback minutes
# into a deployment. Retry the transient classes; a 404 or a checksum mismatch is
# still fatal, because those mean the selection is wrong rather than unlucky.
DOWNLOAD_ATTEMPTS = int(os.environ.get("ASR_DOWNLOAD_ATTEMPTS", "5"))
RETRY_BACKOFF_S = float(os.environ.get("ASR_DOWNLOAD_BACKOFF_S", "5"))
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class ModelFetchError(RuntimeError):
    """A model could not be fetched, verified, or laid out."""


def log(message: str) -> None:
    print(f"[fetch_model] {message}", flush=True)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def download(url: str, target: Path) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=300) as response, target.open("wb") as out:
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def download_with_retries(
    url: str,
    target: Path,
    label: str,
    attempts: int = DOWNLOAD_ATTEMPTS,
    sleep=time.sleep,
) -> str:
    """Download with backoff, returning the SHA256 of what arrived.

    Each attempt rewrites ``target`` from scratch and re-digests it, so a partial
    body from a failed attempt cannot contribute to the checksum.
    """
    last_reason = "unknown"
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return download(url, target)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS:
                raise ModelFetchError(f"{label}: HTTP {exc.code} for {url}") from exc
            last_reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"

        if attempt >= max(1, attempts):
            break
        delay = RETRY_BACKOFF_S * attempt
        log(f"{label}: {last_reason}; retrying in {delay:.0f}s ({attempt} of {attempts})")
        sleep(delay)

    raise ModelFetchError(
        f"{label}: download failed after {attempts} attempt(s): {last_reason}. The host "
        "may be rate limiting or temporarily unavailable; retry the deployment."
    )


def fetch_verified(url: str, expected_sha256: str, target: Path, label: str) -> None:
    if not url:
        raise ModelFetchError(f"{label}: no URL configured")
    if not expected_sha256:
        raise ModelFetchError(
            f"{label}: no SHA256 pinned. Pin the checksum in models/catalog.json "
            "before building an image."
        )
    log(f"{label}: downloading {url}")
    actual = download_with_retries(url, target, label)
    if actual.lower() != expected_sha256.lower():
        raise ModelFetchError(
            f"{label}: SHA256 mismatch. expected={expected_sha256} actual={actual}"
        )
    log(f"{label}: SHA256 verified ({target.stat().st_size} bytes)")


def extract(archive: Path, kind: str, strip_components: int, dest: Path) -> Path:
    if kind not in ("tar.bz2", "tar.gz"):
        raise ModelFetchError(f"unsupported archive type: {kind}")
    mode = "r:bz2" if kind == "tar.bz2" else "r:gz"
    staging = dest / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode) as tar:
        tar.extractall(staging, filter="data")
    root = staging
    for _ in range(strip_components):
        entries = [child for child in root.iterdir() if not child.name.startswith(".")]
        if len(entries) != 1 or not entries[0].is_dir():
            break
        root = entries[0]
    return root


def place_model(extracted: Path, env: dict[str, str], dest: Path) -> None:
    for env_key, canonical in CANONICAL_NAMES.items():
        source_name = env[env_key]
        source = extracted / source_name
        if not source.is_file():
            available = sorted(child.name for child in extracted.iterdir())
            raise ModelFetchError(
                f"model file {source_name!r} is not in the archive. Present: {available}"
            )
        shutil.move(str(source), str(dest / canonical))
        log(f"placed {source_name} -> {canonical}")

    for license_file in sorted(extracted.glob("LICENSE*")):
        shutil.move(str(license_file), str(dest / license_file.name))

    test_wavs = extracted / "test_wavs"
    if test_wavs.is_dir():
        wavs = sorted(test_wavs.glob("*.wav"))
        if wavs:
            (dest / "test_wavs").mkdir(exist_ok=True)
            shutil.move(str(wavs[0]), str(dest / "test_wavs" / "warm.wav"))
            log("kept one test WAV for the /ready and /validate warm decode")


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fetch_model.py")
    parser.add_argument("--env-file", default="/build/model.env")
    parser.add_argument("--dest", default="/opt/models")
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        env = read_env_file(env_path)
    except OSError as exc:
        log(f"ERROR: cannot read {env_path}: {exc}")
        return 1

    missing = [key for key in CANONICAL_NAMES if key not in env]
    if missing:
        log(f"ERROR: {env_path} is missing {missing}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        try:
            archive_kind = env.get("ASR_MODEL_ARCHIVE", "tar.bz2")
            archive = tmpdir / f"model.{archive_kind}"
            fetch_verified(
                env.get("ASR_MODEL_URL", ""),
                env.get("ASR_MODEL_SHA256", ""),
                archive,
                f"asr model {env.get('ASR_MODEL_ID', '?')}",
            )
            extracted = extract(
                archive,
                archive_kind,
                int(env.get("ASR_MODEL_STRIP_COMPONENTS", "1")),
                dest,
            )
            place_model(extracted, env, dest)

            speaker_url = env.get("ASR_SPEAKER_MODEL_URL", "")
            if speaker_url:
                speaker_tmp = tmpdir / SPEAKER_MODEL_NAME
                fetch_verified(
                    speaker_url,
                    env.get("ASR_SPEAKER_MODEL_SHA256", ""),
                    speaker_tmp,
                    f"speaker model {env.get('ASR_SPEAKER_MODEL_ID', '?')}",
                )
                shutil.move(str(speaker_tmp), str(dest / SPEAKER_MODEL_NAME))
                log(f"placed speaker embedding model -> {SPEAKER_MODEL_NAME}")
            else:
                log("no speaker model selected: diarization will be unavailable")

            segmentation_url = env.get("ASR_SEGMENTATION_MODEL_URL", "")
            if segmentation_url:
                label = f"segmentation model {env.get('ASR_SEGMENTATION_MODEL_ID', '?')}"
                kind = env.get("ASR_SEGMENTATION_MODEL_ARCHIVE", "tar.bz2")
                member = env.get("ASR_SEGMENTATION_MODEL_FILE", "model.onnx")
                if kind == "onnx":
                    target = tmpdir / SEGMENTATION_MODEL_NAME
                    fetch_verified(
                        segmentation_url,
                        env.get("ASR_SEGMENTATION_MODEL_SHA256", ""),
                        target,
                        label,
                    )
                    shutil.move(str(target), str(dest / SEGMENTATION_MODEL_NAME))
                else:
                    archive = tmpdir / f"segmentation.{kind}"
                    fetch_verified(
                        segmentation_url,
                        env.get("ASR_SEGMENTATION_MODEL_SHA256", ""),
                        archive,
                        label,
                    )
                    extracted = extract(
                        archive,
                        kind,
                        int(env.get("ASR_SEGMENTATION_MODEL_STRIP_COMPONENTS", "1")),
                        tmpdir,
                    )
                    source = extracted / member
                    if not source.is_file():
                        available = sorted(child.name for child in extracted.iterdir())
                        raise ModelFetchError(
                            f"{label}: {member!r} is not in the archive. Present: {available}"
                        )
                    shutil.move(str(source), str(dest / SEGMENTATION_MODEL_NAME))
                    for license_file in sorted(extracted.glob("LICENSE*")):
                        suffix = license_file.suffix
                        shutil.move(
                            str(license_file), str(dest / f"LICENSE.segmentation{suffix}")
                        )
                log(f"placed segmentation model -> {SEGMENTATION_MODEL_NAME}")
            else:
                log("no segmentation model selected: one speaker per endpointed utterance")
        except (ModelFetchError, OSError, tarfile.TarError) as exc:
            log(f"ERROR: {exc}")
            return 1

    shutil.rmtree(dest / "_staging", ignore_errors=True)
    kept = sorted(child.name for child in dest.iterdir())
    log(f"model directory ready: {kept}")
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
