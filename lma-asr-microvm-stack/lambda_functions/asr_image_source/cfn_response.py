# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""CloudFormation custom-resource response helper.

The runtime injects a ``cfnresponse`` module only into inline ``Code: ZipFile``
functions. This function needs a real deployment package (it imports its own
modules and exceeds the 4 KB inline limit), so the same contract is implemented
here.
"""

from __future__ import annotations

import json
import urllib.request

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(
    event: dict,
    context: object,
    status: str,
    data: dict | None = None,
    physical_resource_id: str | None = None,
    reason: str = "",
) -> None:
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason
            or f"See CloudWatch log stream: {getattr(context, 'log_stream_name', 'n/a')}",
            "PhysicalResourceId": physical_resource_id
            or event.get("PhysicalResourceId")
            or getattr(context, "log_stream_name", "unknown"),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": data or {},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(f"CloudFormation response status: {response.status}")
