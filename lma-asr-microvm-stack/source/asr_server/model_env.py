# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Load the image's baked ``model.env`` into the process environment.

The AsrImageSource custom resource writes the resolved bundle's settings into
``model.env`` inside the image source zip, and the Dockerfile copies it next to
the model weights. That file is what makes an image self-describing: it carries
the engine the baked model needs (``ASR_ENGINE``) and the bundle's calibrated
diarization operating point (``ASR_SPEAKER_THRESHOLD``, ``ASR_MIN_SEGMENT_MS``).

Nothing turns a file into environment variables by itself, and both the warmup
step and the server select their engine from ``$ASR_ENGINE`` — so before this
loader existed the values were written and never read: an ``accurate`` bundle
warmed the STREAMING recognizer and failed the image build loading offline
weights, and a MicroVM fell back to the 0.5 threshold default instead of the
bundle's measured one.

``setdefault`` semantics on purpose: a variable already present in the
environment (the MicroVM's ``EnvironmentVariables``, a session's wire config
resolving later, or a developer's shell) always wins over the baked default.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

DEFAULT_MODEL_ENV_NAME = "model.env"


def load_model_env(model_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Apply ``model.env`` beside the model weights as environment defaults.

    Returns the variables actually applied (absent from the environment before,
    present after), which is also what makes the behaviour testable.
    """
    root = Path(model_dir or os.environ.get("ASR_MODEL_DIR", "/opt/models"))
    path = root / DEFAULT_MODEL_ENV_NAME
    if not path.is_file():
        return {}

    applied: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    if applied:
        _LOG.info(
            "model.env applied %d default(s) from %s: %s",
            len(applied),
            path,
            ", ".join(sorted(applied)),
        )
    return applied
