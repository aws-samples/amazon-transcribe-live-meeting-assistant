#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Per-meeting usage metering and the cost estimate built from it.

:mod:`lma_metering.records` is the writing half -- what a component calls when
it has used something billable on a meeting's behalf. :mod:`lma_metering.pricing`
is the reading half, which joins those records with the price table held in the
configuration store. See ``docs/meeting-cost-estimate.md``.

Shipped in the configuration layer because it is pure Python on the runtime's
own boto3, like :mod:`lma_config`, and because the price table it reads lives in
that store.
"""

from .pricing import (
    DEFAULT_NON_CHARGEABLE_UNITS,
    aggregate,
    estimate_call_cost,
    prices_for_region,
    read_records,
)
from .records import (
    BEDROCK_TOKEN_UNITS,
    CONTEXTS,
    COUNTER_UNIT_PREFIX,
    EXPIRATION_ENV_VAR,
    PK_PREFIX,
    TABLE_ENV_VAR,
    MeteringWriter,
    bedrock_service,
    bedrock_usage,
    build_item,
    comprehend_units,
    counter_usage,
    metering_pk,
)

__all__ = [
    "BEDROCK_TOKEN_UNITS",
    "CONTEXTS",
    "COUNTER_UNIT_PREFIX",
    "DEFAULT_NON_CHARGEABLE_UNITS",
    "EXPIRATION_ENV_VAR",
    "PK_PREFIX",
    "TABLE_ENV_VAR",
    "MeteringWriter",
    "aggregate",
    "bedrock_service",
    "bedrock_usage",
    "build_item",
    "comprehend_units",
    "counter_usage",
    "estimate_call_cost",
    "metering_pk",
    "prices_for_region",
    "read_records",
]
