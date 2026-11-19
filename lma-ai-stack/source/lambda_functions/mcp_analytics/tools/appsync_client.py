# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""One GraphQL request to LMA's AppSync API, signed with this function's IAM role.

The same request start_meeting_now and get_virtual_participant_status make.
AppSync treats the role as a backend caller, so the tool making the request
checks the MCP caller's access first (tools/access.py).
"""

import json
import os
from typing import Any, Dict, Optional

import boto3


def graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The `data` of the response; the first error's message is raised as ValueError."""
    # pylint: disable=import-outside-toplevel
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    url = os.environ.get("APPSYNC_GRAPHQL_URL")
    if not url:
        raise ValueError("APPSYNC_GRAPHQL_URL environment variable not set")
    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise ValueError("No AWS credentials to sign the AppSync request with")
    body = json.dumps({"query": query, "variables": variables or {}})
    request = AWSRequest(
        method="POST", url=url, data=body, headers={"Content-Type": "application/json"}
    )
    SigV4Auth(credentials, "appsync", session.region_name).add_auth(request)
    response = requests.post(url, headers=dict(request.headers), data=body, timeout=(5, 30))
    response.raise_for_status()
    result = response.json()
    if result.get("errors"):
        raise ValueError(result["errors"][0].get("message") or "The request failed")
    return result.get("data") or {}
