# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Live guidance research requests, sent straight to a deployed meeting assistant.

The live guidance analyzer has the meeting assistant function research the
claims and terms its runs find (``action: guidance_research``; see
docs/live-guidance.md#fact-checks-and-lookups). This probe sends that request
itself, with the deployed function, model and tools, so the research step can
be tested on any stack without turning fact checks on for the deployment:

* ``research(...)`` invokes the function with one batch and returns its answer;
* ``metering_contexts(call_id)`` reads what the call was billed as;
* ``cleanup(call_id)`` removes the metering records the probe's synthetic
  CallIds leave (no meeting row is ever created for them).

Which sources a stack can offer is read from the function's own configuration
(``sources_available``): a document knowledge base, the transcript knowledge
base behind past meetings, and a web search key.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config
from ws_stream_probe import _stack_resources

RESEARCH_TIMEOUT_SECONDS = 150


class Research:
    """The deployed meeting assistant function, and the event-sourcing table it meters into."""

    def __init__(self, stack_name: str, region: str | None):
        self.session = boto3.Session(region_name=region)
        cfn = self.session.client("cloudformation")
        ai_stack = next(
            r["PhysicalResourceId"]
            for r in _stack_resources(cfn, stack_name)
            if r["LogicalResourceId"] == "AISTACK"
        )
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
        self.table = ai["EventSourcingTable"]
        self.function_name = f"STRANDS-{stack_name}-MeetingAssist"
        self.lam = self.session.client(
            "lambda",
            config=Config(read_timeout=RESEARCH_TIMEOUT_SECONDS, retries={"max_attempts": 0}),
        )
        self.ddb = self.session.client("dynamodb")

    def exists(self) -> bool:
        try:
            self.lam.get_function_configuration(FunctionName=self.function_name)
        except self.lam.exceptions.ResourceNotFoundException:
            return False
        return True

    def sources_available(self) -> dict[str, bool]:
        """What this stack's meeting assistant can search, from its own configuration."""
        env = (
            self.lam.get_function_configuration(FunctionName=self.function_name)
            .get("Environment", {})
            .get("Variables", {})
        )
        return {
            "documents": bool(env.get("KB_ID")),
            "pastMeetings": bool(env.get("TRANSCRIPT_KB_ID")),
            "webSearch": bool(env.get("TAVILY_API_KEY_SECRET_ARN")),
        }

    @staticmethod
    def new_call_id(label: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"integ-test-research-{label}-{stamp}-{uuid.uuid4().hex[:8]}"

    def research(self, call_id: str, candidates: list[dict], **fields: Any) -> dict:
        """One research request, as the analyzer sends it, and the function's answer."""
        request = {
            "action": "guidance_research",
            "callId": call_id,
            "ownerEmail": fields.pop("ownerEmail", ""),
            "modelId": fields.pop("modelId", "global.anthropic.claude-haiku-5-5"),
            "candidates": candidates,
            "sources": fields.pop("sources", {"documents": True, "pastMeetings": True, "webSearch": True}),
            "mcpTools": fields.pop("mcpTools", []),
            "maxToolCalls": fields.pop("maxToolCalls", 4),
            "deadlineSeconds": fields.pop("deadlineSeconds", 60),
            **fields,
        }
        response = self.lam.invoke(
            FunctionName=self.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(request).encode("utf-8"),
        )
        if response.get("FunctionError"):
            raise RuntimeError(f"the meeting assistant failed: {response['Payload'].read()[:500]!r}")
        return json.loads(response["Payload"].read() or b"{}")

    def _metering_rows(self, call_id: str) -> list[dict]:
        rows, start = [], None
        while True:
            kwargs = {
                "TableName": self.table,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": f"mtr#{call_id}"}},
                "ConsistentRead": True,
            }
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.ddb.query(**kwargs)
            rows.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                return rows

    def metering_contexts(self, call_id: str) -> list[str]:
        return sorted({row.get("Context", {}).get("S") for row in self._metering_rows(call_id)} - {None})

    def cleanup(self, call_id: str) -> None:
        for row in self._metering_rows(call_id):
            self.ddb.delete_item(TableName=self.table, Key={"PK": row["PK"], "SK": row["SK"]})
