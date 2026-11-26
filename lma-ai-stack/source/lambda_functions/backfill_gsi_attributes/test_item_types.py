# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Which rows the backfill stamps for TypeDateIndex (no AWS)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _load():
    spec = importlib.util.spec_from_file_location(
        "backfill_gsi_attributes_index", Path(__file__).resolve().parent / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()


@pytest.mark.parametrize(
    "pk,sk,expected",
    [
        ("cls#2026-11-26#s#00", "ts#2026-11-26T10:00:00Z#id#x", "call"),
        ("pmc#ana@example.com", "ac#20261126100000-aaaaaaaaaaaa", "advance_consent"),
        ("pmc#ana@example.com", "account-names", None),
        ("c#Weekly", "c#Weekly", None),
        ("acn#ana", "ana silva\tana@example.com", None),
    ],
)
def test_only_meeting_list_rows_and_consents_given_in_advance_are_stamped(pk, sk, expected) -> None:
    assert index.item_type_of(pk, sk) == expected
