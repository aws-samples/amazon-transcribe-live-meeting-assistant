# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Put the configuration layer on the path for the tests beside it.

The function carries the configuration layer (for lma_meeting_preparation, the
rules a Join again's meeting preparation is checked by, and lma_sessions), which Lambda puts on
the path at /opt/python; here it is the layer's source directory.
"""

import sys
from pathlib import Path

LAYER = Path(__file__).resolve().parents[1].parent / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))
