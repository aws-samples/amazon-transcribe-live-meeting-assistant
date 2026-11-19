# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Fakes for the meeting tool tests: the tables, AppSync and Lambda, and test rows.

Nothing here reaches AWS. The fixtures that install these are in conftest.py.
"""

from typing import Any, Dict, List

OWNER = "owner@example.com"
SHARED = "shared@example.com"
OTHER = "other@example.com"
ADMIN = "admin@example.com"


class FakeTable:
    """A DynamoDB table: get_item by key, query by PK, scan of everything."""

    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.queries: List[Dict[str, Any]] = []

    def put(self, item: Dict[str, Any]) -> None:
        self.items.append(item)

    def get_item(self, Key, **_kwargs):  # noqa: N803 - boto3's argument name
        for item in self.items:
            if all(item.get(name) == value for name, value in Key.items()):
                return {"Item": item}
        return {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        condition = kwargs["KeyConditionExpression"]
        pk = condition.get_expression()["values"][1]
        rows = [item for item in self.items if item.get("PK") == pk]
        rows.sort(
            key=lambda item: str(item.get("SK")), reverse=not kwargs.get("ScanIndexForward", True)
        )
        if "Limit" in kwargs:
            rows = rows[: kwargs["Limit"]]
        return {"Items": rows}

    def scan(self, **kwargs):
        rows = list(self.items)
        condition = kwargs.get("FilterExpression")
        if condition is not None:
            expression = condition.get_expression()
            name, value = expression["values"][0].name, expression["values"][1]
            rows = [item for item in rows if item.get(name) == value]
        return {"Items": rows}


class Recorder:
    """Records AppSync requests or Lambda invocations; answers with `answers`."""

    def __init__(self):
        self.calls: List[tuple] = []
        self.answers: Dict[str, Any] = {}

    def graphql(self, query, variables=None):
        self.calls.append((query, variables))
        for name, answer in self.answers.items():
            if name in query:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return {}

    def invoke(self, function_name, payload, read_timeout=70):
        self.calls.append((function_name, payload))
        answer = self.answers.get(function_name)
        if isinstance(answer, Exception):
            raise answer
        return answer


def meeting_row(call_id="m1", status="STARTED", **extra) -> Dict[str, Any]:
    """A meeting's row, owned by OWNER and shared with SHARED."""
    row = {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Owner": OWNER,
        "SharedWith": [SHARED],
        "Status": status,
        "CreatedAt": "2026-11-19T13:45:00.000Z",
        "ExpiresAfter": 1900000000,
    }
    row.update(extra)
    return row


def caller(user=OWNER, admin=False, scope="read-write"):
    from tools.access import Caller

    return Caller(user_id=user, is_admin=admin, scope=scope)
