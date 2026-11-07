# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""No unit test under lma-ai-stack/ sends a request to AWS.

Covers the Lambda functions' and layers' tests (each run from its own directory
by ``make test-lambdas``; this directory's pyproject.toml makes it their pytest
rootdir, so this file is loaded for them) and ``test/``. See
scripts/testing/no_real_aws.py for what the guard does and how a test opts in.
Nothing deployed is packaged from this directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GUARD_DIR = Path(__file__).resolve().parents[1] / "scripts" / "testing"
if str(_GUARD_DIR) not in sys.path:
    sys.path.append(str(_GUARD_DIR))

from no_real_aws import *  # noqa: E402,F403 -- the guard's hook and autouse fixture
from no_real_aws import activate  # noqa: E402

activate()
