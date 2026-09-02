# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Make this suite runnable without the AWS SDK installed.

The handler builds an S3 client at import time, so importing it needs *a* boto3.
Nothing under test calls AWS (the tests that exercise ``build`` stub the client),
so a placeholder module is enough — and it means the suite matches what the
Makefile already promises about these tests: no AWS required.
"""

from __future__ import annotations

import sys
import types


def _install_stub(name: str, **attributes: object) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module


try:  # pragma: no cover - depends on the local environment
    import boto3  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    _install_stub("boto3", client=lambda *_args, **_kwargs: None)

try:  # pragma: no cover
    import cfn_response  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    _install_stub(
        "cfn_response",
        SUCCESS="SUCCESS",
        FAILED="FAILED",
        send=lambda *_args, **_kwargs: None,
    )
