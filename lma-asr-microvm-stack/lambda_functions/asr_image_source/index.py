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

Selection is by BUNDLE, not by individual model. A bundle names the ASR model, the
speaker embedder and the turn-detection model *together with the operating point
measured for that combination*, because the threshold is not a property of the
embedder alone: utterance length moves it as much as the model does. Picking the
three models separately let a deployment assemble a combination nobody had ever
measured, and the CloudFormation default threshold (0.2) then silently disagreed
with the catalog's own measured value for the default embedder (0.4).

Returns:
    SourceUri            s3:// URI for AWS::Lambda::MicrovmImage CodeArtifact
    SourceLocation       bucket/key form
    BundleId             resolved bundle id
    ModelId              resolved model id
    SpeakerModelId       resolved speaker model id
    SegmentationModelId  resolved speaker-turn detection model id
    DiarizationAvailable "true" when a speaker model is baked into the image
    TurnDetectionAvailable "true" when a segmentation model is baked into the image
    SpeakerModelMeasured "true" when the bundle carries a calibrated threshold
    SpeakerThreshold     the bundle's calibrated threshold, or "" when uncalibrated
    MinSegmentMs         shortest utterance worth embedding, for this bundle
    BaselineMemoryMiB    memory the bundle was sized for
    NumThreads           inference threads implied by that memory
    Redistributable      "true" when every weight in the bundle may be redistributed
    ModelLicense         licence of the resolved model, for the stack outputs
    LicenceSummary       one-line licence summary for the whole bundle

Every model is a curated catalog entry: there is no parameter for supplying a URL,
so a new model is added by editing catalog.json (with its checksum pinned and its
operating point measured) rather than by a deploy-time override.
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


# Memory is allocated at 2 GiB per vCPU, and inference threads are matched to the
# vCPU count. Kept here rather than as a CloudFormation Mapping because the memory
# now comes from the bundle, and a Mapping cannot be keyed on a resolved value.
_THREADS_BY_MEMORY_MIB = {4096: 2, 8192: 4, 16384: 8}


def _threads_for(memory_mib: int) -> int:
    return _THREADS_BY_MEMORY_MIB.get(memory_mib, max(1, memory_mib // 2048))


def resolve(properties: dict, catalog: dict) -> dict:
    bundle_id = _prop(properties, "BundleId") or catalog.get("defaultBundleId", "")
    bundles = catalog.get("bundles", [])
    if not bundles:
        raise ResolutionError("catalog.json defines no bundles")
    bundle = dict(_find(bundles, bundle_id))

    model_id = bundle.get("modelId", "")
    speaker_id = bundle.get("speakerModelId", "none")

    memory_mib = int(bundle.get("baselineMemoryMiB", 8192))
    # The template needs the memory as a CloudFormation-typed number for
    # MinimumMemoryInMiB, so it carries its own copy. Cross-check them: a silent
    # disagreement would size the MicroVM for one bundle and thread the inference
    # for another.
    declared = _prop(properties, "BaselineMemoryMiB")
    if declared and int(declared) != memory_mib:
        raise ResolutionError(
            f"bundle {bundle_id!r} is sized for {memory_mib} MiB but the stack passed "
            f"{declared} MiB. Update the BundleMemory mapping in template.yaml to match "
            "catalog.json."
        )

    model = dict(_find(catalog.get("models", []), model_id))
    files = dict(model.get("files") or {})

    engine = model.get("engine", "streaming")
    if engine not in ("streaming", "accurate"):
        raise ResolutionError(
            f"model {model.get('id')!r} uses the {engine!r} engine; this stack supports "
            "'streaming' (frame-synchronous) and 'accurate' (offline, VAD-segmented)"
        )
    if not model.get("url"):
        raise ResolutionError(f"model {model.get('id')!r} has no download URL")
    if not model.get("sha256"):
        raise ResolutionError(
            f"model {model.get('id')!r} has no pinned SHA256. Pin it in catalog.json; "
            "unverified weights are never baked into an image."
        )
    missing = [key for key in FILE_KEYS if not files.get(key)]
    if missing:
        raise ResolutionError(f"model {model.get('id')!r} is missing file names: {missing}")
    if not model.get("sherpaOnnx") or not model.get("onnxruntime"):
        raise ResolutionError(
            f"model {model.get('id')!r} must pin sherpaOnnx and onnxruntime versions"
        )

    # An offline model cannot stream: audio has to be cut into utterances by a VAD and
    # each closed utterance decoded. Without one the engine would have no way to
    # decide when an utterance ended, so refuse rather than build an image that
    # cannot transcribe.
    vad_id = bundle.get("vadModelId", "none")
    if engine == "accurate":
        if vad_id in ("none", ""):
            raise ResolutionError(
                f"bundle {bundle_id!r} uses the offline 'accurate' engine, which needs a "
                "vadModelId to segment audio into utterances"
            )
        vad = dict(_find(catalog.get("vadModels", []), vad_id))
        if not vad.get("sha256"):
            raise ResolutionError(f"VAD model {vad.get('id')!r} has no pinned SHA256")
    else:
        if vad_id not in ("none", ""):
            raise ResolutionError(
                f"bundle {bundle_id!r} names a VAD model but its ASR model is streaming; "
                "the streaming engine endpoints internally and never loads one"
            )
        vad = {"id": "none", "url": "", "sha256": "", "license": "n/a"}

    speaker = dict(_find(catalog.get("speakerModels", []), speaker_id))
    if speaker.get("url") and not speaker.get("sha256"):
        raise ResolutionError(
            f"speaker model {speaker.get('id')!r} has no pinned SHA256"
        )

    segmentation_id = bundle.get("segmentationModelId", "none")
    entries = catalog.get("segmentationModels", [])
    if segmentation_id in ("none", "") or not entries:
        segmentation = {"id": segmentation_id or "none", "url": "", "sha256": "", "license": "n/a"}
    else:
        segmentation = dict(_find(entries, segmentation_id))
    if segmentation.get("url") and not segmentation.get("sha256"):
        raise ResolutionError(
            f"segmentation model {segmentation.get('id')!r} has no pinned SHA256"
        )
    # Turn detection is only meaningful when there is an embedder to identify the
    # turns it finds, so a diarization-less image never carries the extra weights.
    if not speaker.get("url"):
        segmentation = {"id": "none", "url": "", "sha256": "", "license": "n/a"}

    return {
        "bundle": bundle,
        "model": model,
        "speaker": speaker,
        "segmentation": segmentation,
        "vad": vad,
        "memoryMiB": memory_mib,
        "numThreads": _threads_for(memory_mib),
    }


def render_model_env(selection: dict) -> str:
    model = selection["model"]
    speaker = selection["speaker"]
    segmentation = selection.get("segmentation") or {"id": "none"}
    vad = selection.get("vad") or {"id": "none"}
    bundle = selection.get("bundle") or {}
    files = model["files"]
    lines = [
        "# Generated by the LMA AsrImageSource custom resource. Do not edit.",
        f"ASR_BUNDLE_ID={bundle.get('id', '')}",
        # The bundle's own calibrated operating point, baked in so the image has a
        # working default without any stack parameter or DynamoDB override. Blank
        # when this pairing has never been calibrated, which the engine treats as
        # "withhold speaker labels" rather than "guess".
        f"ASR_SPEAKER_THRESHOLD={bundle.get('speakerThreshold', '')}",
        f"ASR_MIN_SEGMENT_MS={bundle.get('minSegmentMs', '')}",
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
        f"ASR_SEGMENTATION_MODEL_ID={segmentation.get('id', 'none')}",
        f"ASR_SEGMENTATION_MODEL_URL={segmentation.get('url', '')}",
        f"ASR_SEGMENTATION_MODEL_SHA256={segmentation.get('sha256', '')}",
        f"ASR_SEGMENTATION_MODEL_ARCHIVE={segmentation.get('archive', 'tar.bz2')}",
        f"ASR_SEGMENTATION_MODEL_FILE={segmentation.get('file', 'model.onnx')}",
        f"ASR_SEGMENTATION_MODEL_STRIP_COMPONENTS={segmentation.get('stripComponents', 1)}",
        f"ASR_SEGMENTATION_MODEL_WINDOW_SEC={segmentation.get('windowSec', 10.0)}",
        f"ASR_SEGMENTATION_MODEL_LICENSE={segmentation.get('license', 'n/a')}",
        f"ASR_VAD_MODEL_ID={vad.get('id', 'none')}",
        f"ASR_VAD_MODEL_URL={vad.get('url', '')}",
        f"ASR_VAD_MODEL_SHA256={vad.get('sha256', '')}",
        f"ASR_VAD_MODEL_LICENSE={vad.get('license', 'n/a')}",
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
    bundle = selection["bundle"]
    threshold = bundle.get("speakerThreshold")
    return key, {
        "SourceUri": f"s3://{dest_bucket}/{key}",
        "SourceLocation": f"{dest_bucket}/{key}",
        "BundleId": bundle.get("id", ""),
        "BundleStatus": bundle.get("status", "uncalibrated"),
        "LicenceSummary": bundle.get("licenceSummary", "unknown"),
        "Redistributable": "true" if bundle.get("redistributable") else "false",
        "ModelId": selection["model"]["id"],
        "ModelLicense": selection["model"].get("license", "unknown"),
        "SpeakerModelId": selection["speaker"].get("id", "none"),
        "DiarizationAvailable": "true" if speaker_url else "false",
        # Whether THIS PAIRING has a calibrated operating point. Not a property of
        # the embedder alone: utterance length moves the threshold as much as the
        # model does, so a threshold is only meaningful for a stated pairing.
        # "false" means any threshold would be a guess, and a guessed threshold
        # fragments one speaker into many or merges several into one.
        "SpeakerModelMeasured": "true" if threshold is not None else "false",
        "SpeakerThreshold": "" if threshold is None else str(threshold),
        "MinSegmentMs": str(bundle.get("minSegmentMs", "")),
        "BaselineMemoryMiB": str(selection["memoryMiB"]),
        "NumThreads": str(selection["numThreads"]),
        "SegmentationModelId": selection["segmentation"].get("id", "none"),
        "TurnDetectionAvailable": "true" if selection["segmentation"].get("url") else "false",
        "AsrEngine": selection["model"].get("engine", "streaming"),
        "VadModelId": selection["vad"].get("id", "none"),
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
