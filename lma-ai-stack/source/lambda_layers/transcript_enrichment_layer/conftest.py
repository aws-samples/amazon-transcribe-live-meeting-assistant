# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Puts the configuration layer on the path for this layer's tests.

Every function that attaches this layer attaches the configuration layer too,
and eventprocessor_utils imports lma_channels from it. A test module inside the
eventprocessor_utils package cannot add the path itself: pytest imports the
package, and with it that import, before the module's own code runs.
"""

import sys
from pathlib import Path

CONFIGURATION_LAYER = Path(__file__).resolve().parent.parent / "configuration_layer"
if str(CONFIGURATION_LAYER) not in sys.path:
    sys.path.insert(0, str(CONFIGURATION_LAYER))
