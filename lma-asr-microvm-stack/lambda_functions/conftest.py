# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""No ASR Lambda test sends a request to AWS; see scripts/testing/no_real_aws.py.

Loaded for every function directory below because pytest.ini here makes this
directory their rootdir. It sits outside the function directories, so it is not
in either deployment package.
"""

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
