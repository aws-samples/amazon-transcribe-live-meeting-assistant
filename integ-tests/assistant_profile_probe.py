# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Helpers for test_assistant_profile.py: ask the meeting assistant, and read what it offered.

The assistant is invoked directly, as the Virtual Participant and the desktop
video test invoke it. What it offered a request is read from its own log line
``MCP servers: offered=<ids> not offered=<ids>``, found by the invocation's
request id in the function's log group.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config

#: The section the tests change, and the settings they set.
SECTION = "meeting_assist"

#: How long a saved Meeting Assistant change can take to reach the assistant:
#: the section's standard cache tier is 300 seconds, plus a margin.
APPLY_TIMEOUT_SECONDS = 420
APPLY_POLL_SECONDS = 20

OFFERED_LINE = re.compile(r"MCP servers: offered=(?P<offered>.*?) not offered=(?P<not_offered>.*)$")


def assistant_function_name(stack_name: str) -> str:
    return f"STRANDS-{stack_name}-MeetingAssist"


def invoke_assistant(
    session: Any, stack_name: str, question: str, *, tool_scope: str = "", user_email: str = ""
) -> Dict[str, Any]:
    """Ask the assistant ``question`` about no meeting in particular.

    ``tool_scope`` makes it an in-meeting request ('meeting_chat',
    'voice_assistant' or 'wake_phrase'). Returns the answer text, the function
    error if any, and the invocation's request id.
    """
    lam = session.client("lambda", config=Config(read_timeout=310, retries={"max_attempts": 0}))
    payload: Dict[str, Any] = {
        "text": question,
        "call_id": f"integ-assistant-profile-{int(time.time())}",
        "conversation_history": [],
        "userEmail": user_email,
    }
    if tool_scope:
        payload["toolScope"] = tool_scope
    response = lam.invoke(FunctionName=assistant_function_name(stack_name), Payload=json.dumps(payload).encode())
    body = json.loads(response["Payload"].read() or b"{}")
    return {
        "request_id": response.get("ResponseMetadata", {}).get("RequestId", ""),
        "function_error": response.get("FunctionError"),
        "message": body.get("message", "") if isinstance(body, dict) else "",
        "body": body,
    }


def assistant_log_group(session: Any, stack_name: str) -> str:
    config = session.client("lambda").get_function_configuration(
        FunctionName=assistant_function_name(stack_name)
    )
    return config.get("LoggingConfig", {}).get("LogGroup") or f"/aws/lambda/{assistant_function_name(stack_name)}"


def offered_mcp_servers(session: Any, log_group: str, request_id: str, started_ms: int,
                        timeout_s: int = 90) -> Optional[Dict[str, set]]:
    """The MCP server ids the assistant offered, and those it loaded and did not, for one request.

    None when the request logged no such line (the request was not allowed MCP
    servers at all, or none is installed).
    """
    logs = session.client("logs")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = logs.filter_log_events(
            logGroupName=log_group,
            startTime=started_ms,
            filterPattern=f'"{request_id}" "MCP servers: offered="',
        ).get("events", [])
        for event in events:
            match = OFFERED_LINE.search(event["message"].strip())
            if match:
                def ids(text: str) -> set:
                    return set() if text.strip() == "none" else {i.strip() for i in text.split(",") if i.strip()}

                return {"offered": ids(match["offered"]), "not_offered": ids(match["not_offered"])}
        time.sleep(5)
    return None


def active_mcp_servers(session: Any, table: str, account_id: str) -> list:
    """The installed servers the assistant can load: active and not waiting for credentials."""
    items = session.client("dynamodb").query(
        TableName=table,
        KeyConditionExpression="AccountId = :a",
        ExpressionAttributeValues={":a": {"S": account_id}},
    ).get("Items", [])
    return [
        {"ServerId": i["ServerId"]["S"], "Name": i.get("Name", {}).get("S", "")}
        for i in items
        if i.get("Status", {}).get("S") == "ACTIVE" and not i.get("CredentialsRequired", {}).get("BOOL")
    ]


def _stack_resource(cfn: Any, stack_name: str, logical_id: str) -> str:
    """A resource's physical id, read from every page of the stack's resources.

    describe_stack_resources returns at most 100 resources, and the AI stack has
    several hundred, so the listing is paged.
    """
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        for resource in page["StackResourceSummaries"]:
            if resource["LogicalResourceId"] == logical_id:
                return resource["PhysicalResourceId"]
    raise LookupError(f"stack {stack_name} has no resource {logical_id}")


def mcp_servers_table(session: Any, stack_name: str) -> str:
    cfn = session.client("cloudformation")
    ai_stack = _stack_resource(cfn, stack_name, "AISTACK")
    return _stack_resource(cfn, ai_stack, "MCPServersTable")


def account_id(session: Any) -> str:
    return session.client("sts").get_caller_identity()["Account"]


def default_session(region: Optional[str]) -> Any:
    return boto3.Session(region_name=region)
