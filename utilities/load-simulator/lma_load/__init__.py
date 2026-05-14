# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""LMA Load Simulator.

A pluggable, tag-driven tool for stress-testing a deployed Amazon Transcribe
Live Meeting Assistant (LMA) stack. Provides four scenarios:

* ``concurrent`` — N in-flight meetings for Y minutes, using one of
  four drivers (``kinesis``, ``upload``, ``websocket``, ``vp``).
* ``backfill`` — Seed large historical meeting catalogs with backdated
  timestamps via Kinesis (default) or direct DynamoDB writes.
* ``rbac`` — Provision N synthetic Cognito users + share-matrix and
  measure list/subscribe latency by role.
* ``cleanup`` — Remove every synthetic resource tagged with a given run-id.

Every synthetic row/user/VP carries a ``LoadTestRunId`` tag / attribute /
Owner-prefix so deterministic teardown is always possible.
"""

__version__ = "0.1.0"
