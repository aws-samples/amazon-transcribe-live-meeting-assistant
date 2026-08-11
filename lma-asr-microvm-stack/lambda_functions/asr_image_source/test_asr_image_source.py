# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the ASR image-source packager (no AWS calls).

This custom resource is the mechanism that makes the model a CloudFormation
parameter, so its two contracts matter: an unverifiable model must NEVER reach an
image build, and the published key must change when (and only when) the selection
changes, because that is what makes CloudFormation rebuild the image.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

with mock.patch("boto3.client"):
    import index  # noqa: E402

CATALOG = {
    "version": 1,
    "defaultModelId": "model-a",
    "defaultSpeakerModelId": "spk-a",
    "models": [
        {
            "id": "model-a",
            "engine": "streaming",
            "url": "https://example.invalid/model-a.tar.bz2",
            "sha256": "a" * 64,
            "archive": "tar.bz2",
            "stripComponents": 1,
            "files": {
                "encoder": "encoder.int8.onnx",
                "decoder": "decoder.int8.onnx",
                "joiner": "joiner.int8.onnx",
                "tokens": "tokens.txt",
            },
            "license": "Example License",
            "sherpaOnnx": "1.13.4",
            "onnxruntime": "1.27.0",
        },
        {
            "id": "offline-model",
            "engine": "accurate",
            "url": "https://example.invalid/offline.tar.bz2",
            "sha256": "b" * 64,
            "files": {
                "encoder": "e.onnx",
                "decoder": "d.onnx",
                "joiner": "j.onnx",
                "tokens": "t.txt",
            },
            "sherpaOnnx": "1.13.4",
            "onnxruntime": "1.27.0",
        },
    ],
    "speakerModels": [
        {"id": "spk-a", "url": "https://example.invalid/spk.onnx", "sha256": "c" * 64, "license": "CC-BY-4.0"},
        {"id": "none", "url": "", "sha256": "", "license": "n/a"},
    ],
}


def make_source_zip(catalog: dict | None = CATALOG, with_model_env: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Dockerfile", "FROM scratch\n")
        zf.writestr("asr_server/ws_server.py", "# server\n")
        if catalog is not None:
            zf.writestr("catalog.json", json.dumps(catalog))
        if with_model_env:
            zf.writestr("model.env", "ASR_MODEL_ID=stale\n")
    return buffer.getvalue()


def members(zip_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {name: zf.read(name).decode() for name in zf.namelist()}


def test_resolve_uses_the_catalog_default_when_no_model_is_named() -> None:
    selection = index.resolve({}, CATALOG)

    assert selection["model"]["id"] == "model-a"
    assert selection["speaker"]["id"] == "spk-a"


def test_resolve_applies_overrides_on_top_of_a_catalog_entry() -> None:
    selection = index.resolve(
        {"ModelId": "model-a", "ModelSha256": "f" * 64, "ModelEncoderFile": "enc.onnx"},
        CATALOG,
    )

    assert selection["model"]["sha256"] == "f" * 64
    assert selection["model"]["files"]["encoder"] == "enc.onnx"
    # Untouched fields still come from the catalog.
    assert selection["model"]["files"]["decoder"] == "decoder.int8.onnx"


def test_resolve_accepts_a_fully_specified_custom_model() -> None:
    selection = index.resolve(
        {
            "ModelId": "Custom",
            "ModelUrl": "https://example.invalid/mine.tar.gz",
            "ModelSha256": "d" * 64,
            "ModelArchive": "tar.gz",
            "ModelEncoderFile": "e.onnx",
            "ModelDecoderFile": "d.onnx",
            "ModelJoinerFile": "j.onnx",
            "ModelTokensFile": "t.txt",
            "SherpaOnnxVersion": "1.13.4",
            "OnnxruntimeVersion": "1.27.0",
        },
        CATALOG,
    )

    assert selection["model"]["archive"] == "tar.gz"
    assert selection["model"]["files"]["tokens"] == "t.txt"


def test_resolve_refuses_a_model_with_no_pinned_checksum() -> None:
    with pytest.raises(index.ResolutionError, match="no pinned SHA256"):
        index.resolve(
            {
                "ModelId": "Custom",
                "ModelUrl": "https://example.invalid/mine.tar.bz2",
                "ModelEncoderFile": "e.onnx",
                "ModelDecoderFile": "d.onnx",
                "ModelJoinerFile": "j.onnx",
                "ModelTokensFile": "t.txt",
                "SherpaOnnxVersion": "1.13.4",
                "OnnxruntimeVersion": "1.27.0",
            },
            CATALOG,
        )


def test_resolve_refuses_a_custom_model_with_no_url() -> None:
    with pytest.raises(index.ResolutionError, match="no download URL"):
        index.resolve({"ModelId": "Custom", "ModelSha256": "d" * 64}, CATALOG)


def test_resolve_refuses_a_custom_model_missing_file_names() -> None:
    with pytest.raises(index.ResolutionError, match="missing file names"):
        index.resolve(
            {
                "ModelId": "Custom",
                "ModelUrl": "https://example.invalid/mine.tar.bz2",
                "ModelSha256": "d" * 64,
                "SherpaOnnxVersion": "1.13.4",
                "OnnxruntimeVersion": "1.27.0",
            },
            CATALOG,
        )


def test_resolve_refuses_a_model_without_pinned_runtime_versions() -> None:
    with pytest.raises(index.ResolutionError, match="sherpaOnnx and onnxruntime"):
        index.resolve(
            {
                "ModelId": "Custom",
                "ModelUrl": "https://example.invalid/mine.tar.bz2",
                "ModelSha256": "d" * 64,
                "ModelEncoderFile": "e.onnx",
                "ModelDecoderFile": "d.onnx",
                "ModelJoinerFile": "j.onnx",
                "ModelTokensFile": "t.txt",
            },
            CATALOG,
        )


def test_resolve_rejects_a_non_streaming_engine() -> None:
    with pytest.raises(index.ResolutionError, match="only 'streaming' is supported"):
        index.resolve({"ModelId": "offline-model"}, CATALOG)


def test_resolve_rejects_an_unknown_model_id() -> None:
    with pytest.raises(index.ResolutionError, match="not in the catalog"):
        index.resolve({"ModelId": "nope"}, CATALOG)


def test_resolve_allows_a_transcription_only_image() -> None:
    selection = index.resolve({"SpeakerModelId": "none"}, CATALOG)

    assert selection["speaker"]["url"] == ""


def test_render_model_env_carries_every_value_the_build_reads() -> None:
    rendered = index.render_model_env(index.resolve({}, CATALOG))
    values = dict(
        line.split("=", 1) for line in rendered.splitlines() if "=" in line and not line.startswith("#")
    )

    assert values["ASR_MODEL_ID"] == "model-a"
    assert values["ASR_MODEL_SHA256"] == "a" * 64
    assert values["ASR_MODEL_ENCODER_FILE"] == "encoder.int8.onnx"
    assert values["ASR_SPEAKER_MODEL_URL"] == "https://example.invalid/spk.onnx"
    assert values["SHERPA_ONNX_VERSION"] == "1.13.4"
    assert values["ONNXRUNTIME_VERSION"] == "1.27.0"


def test_rewrite_zip_replaces_model_env_and_keeps_everything_else() -> None:
    rewritten = index.rewrite_zip(make_source_zip(), "ASR_MODEL_ID=fresh\n")
    contents = members(rewritten)

    assert contents["model.env"] == "ASR_MODEL_ID=fresh\n"
    assert contents["Dockerfile"] == "FROM scratch\n"
    assert contents["asr_server/ws_server.py"] == "# server\n"
    # Exactly one model.env, not two.
    with zipfile.ZipFile(io.BytesIO(rewritten)) as zf:
        assert zf.namelist().count("model.env") == 1


def test_rewrite_zip_refuses_a_source_without_a_catalog() -> None:
    with pytest.raises(index.ResolutionError, match="catalog.json is missing"):
        index.rewrite_zip(make_source_zip(catalog=None), "x=1\n")


def test_build_publishes_a_key_derived_from_the_selection() -> None:
    source = make_source_zip()
    uploads: list[dict] = []

    def fake_get_object(Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 kwargs
        return {"Body": io.BytesIO(source)}

    with mock.patch.object(index.s3, "get_object", side_effect=fake_get_object), mock.patch.object(
        index.s3, "put_object", side_effect=lambda **kw: uploads.append(kw)
    ):
        properties = {
            "SourceLocation": "artifacts-bucket/prefix/asr-microvm-src-abc.zip",
            "DestBucket": "stack-bucket",
            "DestPrefix": "image-source",
        }
        first_key, data = index.build(properties)
        second_key, _ = index.build(properties)
        changed_key, changed_data = index.build({**properties, "SpeakerModelId": "none"})

    assert first_key.startswith("image-source/asr-microvm-src-")
    assert first_key.endswith(".zip")
    # Same selection, same key: no needless image rebuild.
    assert second_key == first_key
    # Different selection, different key: CodeArtifact.Uri changes and the image
    # is rebuilt.
    assert changed_key != first_key

    assert data["SourceUri"] == f"s3://stack-bucket/{first_key}"
    assert data["SourceLocation"] == f"stack-bucket/{first_key}"
    assert data["ModelId"] == "model-a"
    assert data["DiarizationAvailable"] == "true"
    assert changed_data["DiarizationAvailable"] == "false"
    assert uploads[0]["Bucket"] == "stack-bucket"
    assert "model.env" in members(uploads[0]["Body"])


def test_build_rejects_a_malformed_source_location() -> None:
    with pytest.raises(index.ResolutionError, match="bucket/key"):
        index.build({"SourceLocation": "just-a-bucket", "DestBucket": "b"})


def test_delete_removes_the_generated_object_and_always_succeeds() -> None:
    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": "image-source/asr-microvm-src-abc.zip",
        "ResourceProperties": {"DestBucket": "stack-bucket"},
        "StackId": "s",
        "RequestId": "r",
        "LogicalResourceId": "l",
        "ResponseURL": "https://example.invalid/cfn",
    }
    with mock.patch.object(index.s3, "delete_object") as delete, mock.patch.object(
        index.cfn_response, "send"
    ) as send:
        index.lambda_handler(event, mock.Mock(log_stream_name="stream"))

    delete.assert_called_once_with(
        Bucket="stack-bucket", Key="image-source/asr-microvm-src-abc.zip"
    )
    assert send.call_args[0][2] == index.cfn_response.SUCCESS


def test_the_shipped_catalog_resolves_with_the_stack_defaults() -> None:
    """Guards the catalog that actually ships, not just the fixture above.

    A malformed or under-specified entry here would only surface as a failed image
    build minutes into a deployment.
    """
    catalog = json.loads((Path(__file__).parents[2] / "source" / "catalog.json").read_text())

    default = index.resolve({}, catalog)
    assert default["model"]["id"] == catalog["defaultModelId"]
    assert default["speaker"]["id"] == catalog["defaultSpeakerModelId"]

    rendered = index.render_model_env(default)
    for key in (
        "ASR_MODEL_URL",
        "ASR_MODEL_SHA256",
        "ASR_MODEL_ENCODER_FILE",
        "ASR_MODEL_DECODER_FILE",
        "ASR_MODEL_JOINER_FILE",
        "ASR_MODEL_TOKENS_FILE",
        "SHERPA_ONNX_VERSION",
        "ONNXRUNTIME_VERSION",
    ):
        line = next(one for one in rendered.splitlines() if one.startswith(f"{key}="))
        assert line.split("=", 1)[1], f"{key} is empty in the shipped catalog"

    # Every catalog entry, not just the default, must be resolvable.
    for entry in catalog["models"]:
        if entry.get("engine") == "streaming":
            index.resolve({"ModelId": entry["id"]}, catalog)
    for entry in catalog["speakerModels"]:
        index.resolve({"SpeakerModelId": entry["id"]}, catalog)


def test_the_shipped_source_zip_layout_survives_the_rewrite() -> None:
    """The real image build context, rewritten, still has what the build reads."""
    source_dir = Path(__file__).parents[2] / "source"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name in ("Dockerfile", "catalog.json", "model.env", "scripts/fetch_model.py"):
            zf.write(source_dir / name, name)

    rewritten = index.rewrite_zip(buffer.getvalue(), "ASR_MODEL_ID=fresh\n")
    contents = members(rewritten)

    # CreateMicrovmImage needs the Dockerfile at the zip ROOT.
    assert "Dockerfile" in contents
    assert contents["model.env"] == "ASR_MODEL_ID=fresh\n"
    assert "scripts/fetch_model.py" in contents


def test_a_failed_build_reports_failed_to_cloudformation() -> None:
    event = {
        "RequestType": "Create",
        "ResourceProperties": {"SourceLocation": "bad", "DestBucket": "b"},
        "StackId": "s",
        "RequestId": "r",
        "LogicalResourceId": "l",
        "ResponseURL": "https://example.invalid/cfn",
    }
    with mock.patch.object(index.cfn_response, "send") as send:
        index.lambda_handler(event, mock.Mock(log_stream_name="stream"))

    assert send.call_args[0][2] == index.cfn_response.FAILED
