# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Drivers that know how to inject a single synthetic meeting into LMA.

Each driver implements the same async contract so scenarios can mix & match:

    async def drive_one(
        meeting_spec: dict,
        stack: LMAStackInfo,
        ctx: RunContext,
    ) -> dict:
        ...

Returning a result dict that includes ``{"callId": ..., "status": "ok"|"error",
"elapsed_ms": float, "errors": [...]}``.
"""
