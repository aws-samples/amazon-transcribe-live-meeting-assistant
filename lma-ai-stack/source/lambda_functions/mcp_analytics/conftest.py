# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Puts the configuration layer the deployed function carries on the path.

The server's index imports the meeting tools, and the guidance tool imports
``lma_notifications`` from that layer, so a test here that imports the real
index needs it, as the tests under tools/ do (tools/conftest.py).
"""

import sys
from pathlib import Path

LAYER = Path(__file__).resolve().parents[2] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))
