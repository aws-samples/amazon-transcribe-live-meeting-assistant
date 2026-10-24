# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Application metric emission, as CloudWatch Embedded Metric Format records.

Deliberately empty of imports, unlike the other packages in this layer, which
re-export their public names here. Two things import from this package and they
have different dependencies available:

* the Lambda functions that emit, which run with the whole layer present;
* ``lma-ai-stack/test/test_main_stack_metrics.py``, a static gate that reads the
  catalogue to check every emitted metric against the dashboard and the alarms
  in ``lma-main.yaml``. It runs under ``make test-ai-stack``, where nothing from
  this layer's ``requirements.txt`` is installed.

Re-exporting from here would make the second case import the first case's
dependencies, so callers name the module they want:

    from metrics_utils.catalogue import CATALOGUE, Component
    from metrics_utils.emf import MetricEmitter
"""
