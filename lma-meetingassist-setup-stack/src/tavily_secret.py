#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Resolve the Tavily API key for the meeting assistant's web search tool.

The key is kept in AWS Secrets Manager and the Lambda is given only the ARN of
the secret, in ``TAVILY_API_KEY_SECRET_ARN``. Lambda has no equivalent of the
ECS agent's ability to inject a secret into the environment at start, so the
function fetches the value itself.

Two properties matter and are the reason this lives in its own module rather
than inline at the call site:

* **The configured / not-configured decision costs nothing.** Whether the web
  search tool is offered to the agent at all is decided by
  :func:`is_configured`, which only looks at whether the ARN is set. A
  deployment with web search switched off therefore never calls Secrets
  Manager, on any invocation.
* **The value is fetched at most once per execution environment.**
  :func:`get_api_key` caches it in a module-level variable, so a warm container
  does not pay a Secrets Manager call per search. A rotated key is picked up by
  the next cold start; see ``docs/meeting-assistant.md`` for the rotation
  procedure.

A failed fetch is deliberately *not* cached: the tool reports itself
unavailable for that one search and the next search tries again, rather than
one throttled call disabling web search for the life of the container.
"""

import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# Set from the TavilyApiKeySecretArn CloudFormation parameter. Empty means the
# deployment configured no Tavily API key.
SECRET_ARN = os.environ.get("TAVILY_API_KEY_SECRET_ARN", "").strip()

# Populated on the first successful fetch and reused for the life of the
# execution environment.
_CACHED_API_KEY: Optional[str] = None

# Created on first use so that importing this module costs nothing in a
# deployment that has no Tavily API key.
_CLIENT = None


def is_configured() -> bool:
    """True when this deployment supplied a Tavily API key.

    Answered from the environment alone — no Secrets Manager call — so it is
    safe to use as the condition for registering the web search tool.
    """
    return bool(SECRET_ARN)


def _client():
    """The Secrets Manager client, created once per execution environment."""
    global _CLIENT  # pylint: disable=global-statement
    if _CLIENT is None:
        _CLIENT = boto3.client("secretsmanager")
    return _CLIENT


def get_api_key() -> str:
    """The Tavily API key, or an empty string when it cannot be supplied.

    Returns an empty string when no key is configured and when the fetch fails.
    Callers treat an empty result as "web search is unavailable right now".
    """
    global _CACHED_API_KEY  # pylint: disable=global-statement
    if _CACHED_API_KEY is not None:
        return _CACHED_API_KEY
    if not SECRET_ARN:
        _CACHED_API_KEY = ""
        return _CACHED_API_KEY

    try:
        response = _client().get_secret_value(SecretId=SECRET_ARN)
    except Exception:  # pylint: disable=broad-except
        # Logged without the ARN's secret value (there is none in an ARN) and
        # without the key, which has not been read.
        logger.exception("Could not read the secret supplying the Tavily API key")
        return ""

    value = (response.get("SecretString") or "").strip()
    if not value:
        logger.warning("The secret supplying the Tavily API key holds no value")
        return ""

    _CACHED_API_KEY = value
    return _CACHED_API_KEY


def reset_cache() -> None:
    """Drop the cached key and client. For tests."""
    global _CACHED_API_KEY, _CLIENT  # pylint: disable=global-statement
    _CACHED_API_KEY = None
    _CLIENT = None
