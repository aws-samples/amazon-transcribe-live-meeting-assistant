# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting deletes the metering records behind its cost estimate.

No AWS: the AppSync client and the GraphQL DSL are replaced before import, and
the table is a fake that pages its query results.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
os.environ.setdefault("LCA_CALL_EVENTS_TABLE", "calls")
os.environ.setdefault("S3_BUCKET_NAME", "bucket")
os.environ.setdefault("S3_RECORDINGS_PREFIX", "rec/")
os.environ.setdefault("S3_TRANSCRIPTS_PREFIX", "trs/")

_appsync = types.ModuleType("appsync_utils")
_appsync.AppsyncRequestsGqlClient = lambda **_kwargs: object()
sys.modules.setdefault("appsync_utils", _appsync)
_gql = types.ModuleType("gql")
_gql.gql = lambda text: text
_dsl = types.ModuleType("gql.dsl")
for _name in ("DSLMutation", "DSLQuery", "DSLSchema", "dsl_gql"):
    setattr(_dsl, _name, object)
sys.modules.setdefault("gql", _gql)
sys.modules.setdefault("gql.dsl", _dsl)

_SPEC = importlib.util.spec_from_file_location("meeting_controls_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)


class FakeTable:
    def __init__(self, keys, page=2):
        self.keys = list(keys)
        self.page = page
        self.deleted = []
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        start = kwargs.get("ExclusiveStartKey", {}).get("i", 0)
        items = self.keys[start : start + self.page]
        out = {"Items": items}
        if start + self.page < len(self.keys):
            out["LastEvaluatedKey"] = {"i": start + self.page}
        return out

    def delete_item(self, Key):  # noqa: N803
        self.deleted.append(Key)


def test_every_metering_record_of_the_meeting_is_deleted(monkeypatch) -> None:
    keys = [{"PK": "mtr#m-1", "SK": f"Summary#t#{i}"} for i in range(5)]
    table = FakeTable(keys)
    monkeypatch.setattr(index, "ddbTable", table)
    assert index.delete_metering_records("m-1") == 5
    assert table.deleted == keys
    assert {q["ExpressionAttributeValues"][":pk"] for q in table.queries} == {"mtr#m-1"}
    assert len(table.queries) == 3


def test_a_meeting_with_no_metering_records_deletes_nothing(monkeypatch) -> None:
    table = FakeTable([])
    monkeypatch.setattr(index, "ddbTable", table)
    assert index.delete_metering_records("m-1") == 0
    assert table.deleted == []
