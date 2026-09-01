# syntax=docker/dockerfile:1
#
# ASR MicroVM image for LMA. Runs the streaming ASR + diarization WebSocket
# server on :8080 behind the Lambda MicroVMs lifecycle hook server on :9000.
#
# ARM64 ONLY (Lambda MicroVMs are Graviton): build with --platform linux/arm64.
#
# The model is NOT committed. It is downloaded and SHA256-verified during this
# build from model.env, which the AsrImageSource custom resource writes from the
# stack's model parameters. See docs/microvm-asr.md.

FROM public.ecr.aws/lambda/microvms:al2023-minimal

# hadolint ignore=DL3041
RUN dnf install -y python3.11 python3.11-pip \
    && dnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /app

COPY model.env /build/model.env

# sherpa-onnx and onnxruntime versions travel with the model: exports differ in
# which runtime can load them, so the catalog entry pins both.
RUN SHERPA="$(sed -n 's/^SHERPA_ONNX_VERSION=//p' /build/model.env | tr -d '"' | head -n1)" \
    && ORT="$(sed -n 's/^ONNXRUNTIME_VERSION=//p' /build/model.env | tr -d '"' | head -n1)" \
    && test -n "${SHERPA}" -a -n "${ORT}" \
    && python3.11 -m pip install --no-cache-dir \
        "sherpa-onnx==${SHERPA}" \
        "onnxruntime==${ORT}" \
        websockets==13.1 \
        pydantic==2.9.2 \
        numpy==1.26.4

# The sherpa-onnx aarch64 wheel does not bundle onnxruntime; its native library
# dlopen()s an unversioned libonnxruntime.so that the onnxruntime wheel does not
# ship. Without this symlink + ld.so registration both the build-time warmup and
# the server fail with "libonnxruntime.so: cannot open shared object file".
RUN ORT_CAPI="$(python3.11 -c 'import onnxruntime, os; print(os.path.join(os.path.dirname(onnxruntime.__file__), "capi"))')" \
    && ln -sf "${ORT_CAPI}"/libonnxruntime.so.* "${ORT_CAPI}/libonnxruntime.so" \
    && printf '%s\n' "${ORT_CAPI}" > /etc/ld.so.conf.d/onnxruntime.conf \
    && ldconfig

COPY scripts/fetch_model.py /build/fetch_model.py
RUN python3.11 /build/fetch_model.py --env-file /build/model.env --dest /opt/models
# Keep the bundle's settings next to the weights they describe: warmup and the
# server load this as environment DEFAULTS (asr_server/model_env.py), which is
# how the baked engine selection and calibrated diarization operating point
# actually reach the process. Explicit environment variables still win.
RUN cp /build/model.env /opt/models/model.env

ENV ASR_MODEL_DIR=/opt/models \
    ASR_HOST=0.0.0.0 \
    ASR_PORT=8080 \
    ASR_NUM_THREADS=4 \
    ASR_LOG_LEVEL=INFO \
    HOOK_PORT=9000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY asr_protocol/ ./asr_protocol/
COPY asr_server/ ./asr_server/
COPY asr_microvm/ ./asr_microvm/

# Fails the build if the model cannot load or the decode path errors, so no
# un-warmed image is ever published.
RUN python3.11 -m asr_server.warmup --model /opt/models

EXPOSE 8080 9000

CMD ["python3.11", "-m", "asr_microvm.hook_server"]
