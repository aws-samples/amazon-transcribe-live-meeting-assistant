# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Regression tests for list_meetings.query_by_date_range — specifically the
GSI sort-key bounds.

The GSI key format is ``ts#<ISO8601>#id#<CallId>``. An earlier upper-bound
of ``"ts#<end_iso>#~"`` (a stray ``#``) caused every real SK whose date was
followed by a ``T`` (i.e. all of them, e.g. ``ts#2026-05-27T17:34:…``) to
be excluded from the ``between(...)`` query, because ``T`` (0x54) sorts
after ``#`` (0x23). The fix is to drop the inner ``#`` so the upper-bound
ends with ``~`` (0x7E), which sorts after every legal byte at that
position.

These tests pin both bounds via ``Mock`` introspection so the bug cannot
silently regress.
"""

import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

import list_meetings  # noqa: E402  (env vars must be set before import)


def _capture_query_bounds(start_iso: str, end_iso: str):
    """Run query_by_date_range with a stub table; return (sk_lo, sk_hi).

    The ``KeyConditionExpression`` is a boto3 ``ConditionBase`` chain. The
    ``between(...)`` clause stores the raw lo/hi as the second and third
    entries in its ``_values`` tuple, after the attribute reference itself.
    """
    captured: dict = {}

    def fake_query(**kwargs):
        captured["kwargs"] = kwargs
        return {"Items": [], "LastEvaluatedKey": None}

    table = MagicMock()
    table.query.side_effect = fake_query

    list_meetings.query_by_date_range(table=table, start_date=start_iso, end_date=end_iso, limit=10)

    cond = captured["kwargs"]["KeyConditionExpression"]

    # The KeyConditionExpression is `Key("ItemType").eq(...) & Key("SK").between(lo, hi)`.
    # Walk the AND tree to find the .between() leaf.
    def find_between(node):
        # Leaf condition: has format string + tuple of values
        values = getattr(node, "_values", None)
        if values and len(values) == 3:
            # between(attr, lo, hi)
            return values[1], values[2]
        # AND/OR node: recurse into operands stored on _values
        if values:
            for child in values:
                hit = find_between(child)
                if hit:
                    return hit
        return None

    bounds = find_between(cond)
    assert bounds is not None, "Could not find between() leaf in KeyConditionExpression"
    return bounds


class TestSortKeyBounds(unittest.TestCase):
    """Pin the exact GSI sort-key bounds used by query_by_date_range."""

    def test_explicit_iso_bounds_have_no_stray_hash(self):
        """The fix: upper bound is `ts#<end>~`, NOT `ts#<end>#~`."""
        sk_lo, sk_hi = _capture_query_bounds("2026-05-27T00:00:00Z", "2026-05-27T23:59:59Z")
        self.assertEqual(sk_lo, "ts#2026-05-27T00:00:00Z")
        self.assertEqual(sk_hi, "ts#2026-05-27T23:59:59Z~")
        # The bug — re-introducing the `#` — would put the upper bound below
        # the literal `T` in real SKs. Guard against it explicitly.
        self.assertFalse(
            sk_hi.endswith("#~"),
            "Upper bound must not end with '#~' — that excludes real SKs whose "
            "date is followed by 'T' (which sorts after '#').",
        )

    def test_date_only_bounds_match_default_caller_path(self):
        """Caller path that triggered the original bug: date-only end_iso."""
        sk_lo, sk_hi = _capture_query_bounds("2026-05-20", "2026-05-27")
        self.assertEqual(sk_lo, "ts#2026-05-20")
        # Critical: with the buggy `#~` suffix this would be `ts#2026-05-27#~`,
        # which sorts BEFORE every real SK like `ts#2026-05-27T…`. With the
        # fix it ends in `~` so all of `2026-05-27`'s meetings are included.
        self.assertEqual(sk_hi, "ts#2026-05-27~")

    def test_real_sk_falls_inside_date_only_range(self):
        """Sanity check: a real SK shape is inside the date-only between() span."""
        sk_lo, sk_hi = _capture_query_bounds("2026-05-27", "2026-05-27")
        real_sk = "ts#2026-05-27T17:34:00.123Z#id#abc-123"
        self.assertLessEqual(sk_lo, real_sk)
        self.assertLessEqual(real_sk, sk_hi)

        # And confirm the OLD buggy bound would have excluded it.
        buggy_hi = "ts#2026-05-27#~"
        self.assertGreater(
            real_sk,
            buggy_hi,
            "Sanity check on the original bug: a real SK must sort *after* the "
            "old buggy `#~` bound, demonstrating why current-day meetings were "
            "being excluded.",
        )


class TestUpperBoundOrdering(unittest.TestCase):
    """Pure-string ordering checks that don't involve boto3."""

    def test_tilde_sorts_after_T(self):
        """The fix relies on `~` (0x7E) > `T` (0x54) > `#` (0x23)."""
        self.assertGreater("~", "T")
        self.assertGreater("T", "#")

    def test_fixed_bound_includes_real_sk_old_bound_excludes(self):
        date = "2026-05-27"
        real_sk = f"ts#{date}T12:00:00Z#id#x"
        fixed_hi = f"ts#{date}~"
        buggy_hi = f"ts#{date}#~"
        self.assertLess(real_sk, fixed_hi, "fix: real SK is inside the range")
        self.assertGreater(real_sk, buggy_hi, "bug: real SK was outside the range")


if __name__ == "__main__":
    unittest.main()
