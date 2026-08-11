# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Build the per-stack ASR MicroVM image source zip.

CreateMicrovmImage takes a code artifact and a base image and has no build-arg
passthrough, so the only way to make the model a CloudFormation parameter is to
put the selection *inside* the code artifact. This custom resource resolves the
stack's model parameters against the catalog shipped in the published source
zip, rewrites ``model.env``, and republishes the zip under a key derived from the
selection. Because the key changes with the selection, changing the model
parameter changes CodeArtifact.Uri and CloudFormation rebuilds the image.

Returns:
    SourceUri            s3:// URI for AWS::Lambda::MicrovmImage CodeArtifact
    SourceLocation       bucket/key form
    ModelId              resolved model id
    SpeakerModelId       resolved speaker model id
    DiarizationAvailable "true" when a speaker model is baked into the image
    ModelLicense         licence of the resolved model, for the stack outputs
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import zipfile

import boto3
import cfn_response

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO"), logging.INFO))

s3 = boto3.client("s3")

CATALOG_MEMBER = "catalog.json"
MODEL_ENV_MEMBER = "model.env"
CUSTOM_MODEL_ID = "Custom"

FILE_KEYS = ("encoder", "decoder", "joiner", "tokens")


class ResolutionError(Exception):
    """The model parameters could not be resolved to a complete selection."""


def _prop(properties: dict, name: str, default: str = "") -> str:
    value = properties.get(name, default)
    return "" if value is None else str(value).strip()


def _find(entries: list, entry_id: str) -> dict:
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    available = ", ".join(sorted(str(entry.get("id")) for entry in entries))
    raise ResolutionError(f"{entry_id!r} is not in the catalog. Available: {available}")


def resolve(properties: dict, catalog: dict) -> dict:
    model_id = _prop(properties, "ModelId") or catalog.get("defaultModelId", "")
    speaker_id = _prop(properties, "SpeakerModelId") or catalog.get(
        "defaultSpeakerModelId", "none"
    )

    if model_id == CUSTOM_MODEL_ID:
        model = {
            "id": _prop(properties, "ModelName") or "custom",
            "engine": "streaming",
            "archive": _prop(properties, "ModelArchive") or "tar.bz2",
            "stripComponents": int(_prop(properties, "ModelStripComponents") or "1"),
            "files": {},
            "license": _prop(properties, "ModelLicense") or "customer-supplied",
            "sherpaOnnx": "",
            "onnxruntime": "",
            "url": "",
            "sha256": "",
        }
    else:
        model = dict(_find(catalog.get("models", []), model_id))

    for name, key in (
        ("ModelUrl", "url"),
        ("ModelSha256", "sha256"),
        ("ModelArchive", "archive"),
        ("SherpaOnnxVersion", "sherpaOnnx"),
        ("OnnxruntimeVersion", "onnxruntime"),
    ):
        override = _prop(properties, name)
        if override:
            model[key] = override

    files = dict(model.get("files") or {})
    for key in FILE_KEYS:
        override = _prop(properties, f"Model{key.capitalize()}File")
        if override:
            files[key] = override
    model["files"] = files

    if model.get("engine", "streaming") != "streaming":
        raise ResolutionError(
            f"model {model.get('id')!r} uses the {model.get('engine')!r} engine; only "
            "'streaming' is supported by this stack"
        )
    if not model.get("url"):
        raise ResolutionError(f"model {model.get('id')!r} has no download URL")
    if not model.get("sha256"):
        raise ResolutionError(
            f"model {model.get('id')!r} has no pinned SHA256. Pin it in catalog.json or "
            "supply AsrModelSha256; unverified weights are never baked into an image."
        )
    missing = [key for key in FILE_KEYS if not files.get(key)]
    if missing:
        raise ResolutionError(f"model {model.get('id')!r} is missing file names: {missing}")
    if not model.get("sherpaOnnx") or not model.get("onnxruntime"):
        raise ResolutionError(
            f"model {model.get('id')!r} must pin sherpaOnnx and onnxruntime versions"
        )

    speaker = dict(_find(catalog.get("speakerModels", []), speaker_id))
    for name, key in (("SpeakerModelUrl", "url"), ("SpeakerModelSha256", "sha256")):
        override = _prop(properties, name)
        if override:
            speaker[key] = override
    if speaker.get("url") and not speaker.get("sha256"):
        raise ResolutionError(
            f"speaker model {speaker.get('id')!r} has no pinned SHA256"
        )

    return {"model": model, "speaker": speaker}


def render_model_env(selection: dict) -> str:
    model = selection["model"]
    speaker = selection["speaker"]
    files = model["files"]
    lines = [
        "# Generated by the LMA AsrImageSource custom resource. Do not edit.",
        f"ASR_MODEL_ID={model['id']}",
        f"ASR_MODEL_URL={model['url']}",
        f"ASR_MODEL_SHA256={model['sha256']}",
        f"ASR_MODEL_ARCHIVE={model.get('archive', 'tar.bz2')}",
        f"ASR_MODEL_STRIP_COMPONENTS={model.get('stripComponents', 1)}",
        f"ASR_MODEL_ENCODER_FILE={files['encoder']}",
        f"ASR_MODEL_DECODER_FILE={files['decoder']}",
        f"ASR_MODEL_JOINER_FILE={files['joiner']}",
        f"ASR_MODEL_TOKENS_FILE={files['tokens']}",
        f"ASR_MODEL_ENGINE={model.get('engine', 'streaming')}",
        f"ASR_MODEL_LICENSE={model.get('license', 'unknown')}",
        f"ASR_SPEAKER_MODEL_ID={speaker.get('id', 'none')}",
        f"ASR_SPEAKER_MODEL_URL={speaker.get('url', '')}",
        f"ASR_SPEAKER_MODEL_SHA256={speaker.get('sha256', '')}",
        f"ASR_SPEAKER_MODEL_LICENSE={speaker.get('license', 'n/a')}",
        f"SHERPA_ONNX_VERSION={model['sherpaOnnx']}",
        f"ONNXRUNTIME_VERSION={model['onnxruntime']}",
        "",
    ]
    return "\n".join(lines)


def rewrite_zip(source: bytes, model_env: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as src, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        if CATALOG_MEMBER not in src.namelist():
            raise ResolutionError(f"{CATALOG_MEMBER} is missing from the source zip")
        for item in src.infolist():
            if item.filename == MODEL_ENV_MEMBER:
                continue
            dst.writestr(item, src.read(item.filename))
        dst.writestr(MODEL_ENV_MEMBER, model_env)
    return out.getvalue()


def read_catalog(source: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(source)) as src:
        return json.loads(src.read(CATALOG_MEMBER))


def build(properties: dict) -> tuple[str, dict]:
    source_bucket, _, source_key = _prop(properties, "SourceLocation").partition("/")
    if not source_bucket or not source_key:
        raise ResolutionError(
            "SourceLocation must be 'bucket/key' for the published image source zip"
        )
    dest_bucket = _prop(properties, "DestBucket")
    dest_prefix = _prop(properties, "DestPrefix") or "asr-microvm-image-source"

    source = s3.get_object(Bucket=source_bucket, Key=source_key)["Body"].read()
    selection = resolve(properties, read_catalog(source))
    model_env = render_model_env(selection)

    fingerprint = hashlib.sha256(f"{source_key}\n{model_env}".encode()).hexdigest()[:16]
    key = f"{dest_prefix}/asr-microvm-src-{fingerprint}.zip"

    s3.put_object(
        Bucket=dest_bucket,
        Key=key,
        Body=rewrite_zip(source, model_env),
        ContentType="application/zip",
    )
    logger.info("Published ASR image source to s3://%s/%s", dest_bucket, key)

    speaker_url = selection["speaker"].get("url", "")
    return key, {
        "SourceUri": f"s3://{dest_bucket}/{key}",
        "SourceLocation": f"{dest_bucket}/{key}",
        "ModelId": selection["model"]["id"],
        "ModelLicense": selection["model"].get("license", "unknown"),
        "SpeakerModelId": selection["speaker"].get("id", "none"),
        "DiarizationAvailable": "true" if speaker_url else "false",
    }


def lambda_handler(event, context):
    request_type = event.get("RequestType")
    properties = event.get("ResourceProperties", {}) or {}
    logger.info("AsrImageSource %s", request_type)

    try:
        if request_type == "Delete":
            physical_id = event.get("PhysicalResourceId", "")
            dest_bucket = _prop(properties, "DestBucket")
            if dest_bucket and physical_id.endswith(".zip"):
                try:
                    s3.delete_object(Bucket=dest_bucket, Key=physical_id)
                except Exception:  # noqa: BLE001 - never block a stack delete
                    logger.exception("Could not delete s3://%s/%s", dest_bucket, physical_id)
            cfn_response.send(event, context, cfn_response.SUCCESS, {}, physical_id)
            return

        key, data = build(properties)
        cfn_response.send(event, context, cfn_response.SUCCESS, data, key)
    except Exception as exc:  # noqa: BLE001 - a failure must reach CloudFormation
        logger.exception("AsrImageSource failed")
        cfn_response.send(
            event,
            context,
            cfn_response.FAILED,
            {},
            event.get("PhysicalResourceId"),
            reason=str(exc)[:900],
        )
