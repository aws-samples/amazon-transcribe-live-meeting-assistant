#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Put this stack's Lambda source on the path for the tests beside it.

WHY THE TESTS ARE NOT IN ``src/``
---------------------------------
Everywhere else in this repository a Lambda's tests sit next to its handler,
because each function has its own ``CodeUri`` and so pays only for its own
tests being in the zip. This stack is different: three functions share one
``Code: ./src``, so a test file there ships in all three deployment packages,
and editing a test changes the code hash of the meeting-assist function —
a function the test has nothing to do with.

So the tests live here, and this shim gives them the sibling-import behaviour
they would have had in ``src/``. ``SOURCE_DIR`` is exported for the handful of
tests that read a module's own text (to assert, for instance, that a config
object is passed and not merely defined).

The same sharing applies to the agent's own modules, which do go into
``src/``: they ship in all three functions' packages and change all three code
hashes. They are a handful of small files rather than a test suite, so that is
left as it is — but giving the troubleshooting function a ``Code:`` directory
of its own is the change that would fix both halves.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The directory packaged as this stack's Lambda code.
SOURCE_DIR = Path(__file__).resolve().parent.parent / "src"

if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))
