# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""No test here sends a request to AWS; see scripts/testing/no_real_aws.py."""

from __future__ import annotations

import sys
from pathlib import Path

_GUARD_DIR = Path(__file__).resolve().parents[2] / "scripts" / "testing"
if str(_GUARD_DIR) not in sys.path:
    sys.path.append(str(_GUARD_DIR))

# The guard's pytest_configure hook and its autouse fixture.
from no_real_aws import *
from no_real_aws import activate

activate()
