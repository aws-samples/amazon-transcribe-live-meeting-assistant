#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The configuration reader for this stack's functions, with or without the layer.

Deployed by lma-main.yaml, the meeting assistant and the troubleshooting agent
carry the AI stack's configuration layer and read their Amazon Bedrock models
with its ``read_setting``: the Configuration page's value first, then the stack
parameter's environment variable, then a default.

This template can also be deployed on its own, with no AI stack and so no
layer. Then ``read_setting`` here is the environment variable or the default,
which is exactly what these functions read before the models were on the
Configuration page. lma-ai-stack/test/test_parameter_backed_settings.py holds the
integrated deployment to carrying the layer, so the fallback below is reached
only by a standalone one.
"""

from __future__ import annotations

import os
from typing import Any

try:
    # pylint: disable-next=import-error
    from lma_config import read_setting  # noqa: F401 -- re-exported; configuration layer
except ImportError:  # deployed on its own, without the AI stack's configuration layer

    def read_setting(  # type: ignore[misc]
        section: str,
        setting: str,
        *,
        env_var: str,
        default: Any = None,
        store: Any = None,
    ) -> Any:
        """The environment variable, or ``default`` when it is unset or empty."""
        del section, setting, store
        return os.environ.get(env_var) or default
