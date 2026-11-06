#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The configuration reader for this stack's functions, with or without the layer.

Deployed by lma-main.yaml, the meeting assistant and the troubleshooting agent
carry the AI stack's configuration layer and read their Amazon Bedrock models
with its ``read_setting``: the Configuration page's value first, then the
setting's shipped default.

This template can also be deployed on its own, with no AI stack and so no
layer. Then ``read_setting`` here is the caller's ``default``, which each caller
keeps equal to the shipped default. lma-ai-stack/test/test_parameter_backed_settings.py
holds the integrated deployment to carrying the layer, so the fallback below is
reached only by a standalone one.
"""

from __future__ import annotations

import os
from typing import Any

try:
    # pylint: disable-next=import-error
    from lma_config import (  # noqa: F401 -- re-exported; configuration layer
        base_model_id,
        model_arn,
        read_setting,
        resolve_model_id,
    )
except ImportError:  # deployed on its own, without the AI stack's configuration layer
    # With no configuration there is no Model inference geography either, so a
    # model id is used as given: the setting's default, Global.
    _PROFILE_PREFIXES = frozenset({"global", "us", "us-gov", "eu", "apac", "jp", "au", "ca", "in"})

    def read_setting(  # type: ignore[misc]
        section: str,
        setting: str,
        *,
        env_var: str = "",
        default: Any = None,
        store: Any = None,
    ) -> Any:
        """The environment variable when one is named and set, else ``default``."""
        del section, setting, store
        return (os.environ.get(env_var) if env_var else None) or default

    def resolve_model_id(model_id: str, *, region: str = "", store: Any = None) -> str:  # type: ignore[misc]
        """``model_id`` unchanged."""
        del region, store
        return model_id

    def base_model_id(model_id: str) -> str:  # type: ignore[misc]
        """The model id with any inference-profile prefix removed."""
        head, _, rest = (model_id or "").partition(".")
        return rest if rest and head in _PROFILE_PREFIXES else model_id

    def model_arn(  # type: ignore[misc]
        model_id: str, region: str, account_id: str, partition: str = "aws"
    ) -> str:
        """The inference-profile or foundation-model ARN of ``model_id``."""
        if base_model_id(model_id) != model_id:
            return f"arn:{partition}:bedrock:{region}:{account_id}:inference-profile/{model_id}"
        return f"arn:{partition}:bedrock:{region}::foundation-model/{model_id}"
