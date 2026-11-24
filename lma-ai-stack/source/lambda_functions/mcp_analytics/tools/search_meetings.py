# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Tool 1: search_lma_meetings
Semantic search across meeting transcripts using Bedrock Knowledge Base
"""

import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional

import boto3

from tools.url_helper import get_meeting_url

logger = logging.getLogger()

# The metadata key that holds a meeting's owner in the transcript knowledge base.
OWNER_METADATA_KEY = "Owner"

# The metadata key that holds a meeting's creation time, as whole seconds since
# the epoch, in the transcript knowledge base (CREATED_AT_EPOCH_KEY in
# bedrock_summary_lambda). startDate and endDate bound a search on it.
CREATED_AT_METADATA_KEY = "CreatedAtEpochSeconds"


def _date_bound(value: str, argument: str, end_of_day: bool) -> int:
    """``value``, an ISO 8601 date or date and time, as seconds since the epoch.

    A date alone stands for the whole day in UTC: its first second as a start,
    its last second as an end. A time without an offset is read as UTC.
    """
    text = value.strip() if isinstance(value, str) else ""
    try:
        try:
            # Every date-only form ISO 8601 has (2026-11-30, 20261130, 2026-W48-1).
            day = datetime.combine(date.fromisoformat(text), time(), tzinfo=timezone.utc)
        except ValueError:
            day = None
        if day is not None:
            bound = day + timedelta(days=1) - timedelta(seconds=1) if end_of_day else day
        else:
            bound = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if bound.tzinfo is None:
                bound = bound.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(
            f"{argument} must be an ISO 8601 date or date and time, "
            "for example 2026-11-01 or 2026-11-01T09:00:00Z"
        ) from None
    return int(bound.timestamp())


def _configured_model_id() -> str:
    """The model the transcript knowledge base answers with.

    The summaries' model: the Amazon Bedrock Models section's value on the
    Configuration page, else the shipped default in setting_defaults.json. Read
    per search, so a change reaches it within the section's cache tier.

    The configuration layer is imported here rather than at the top of the
    module because only this tool reads it: the server's other tools, and their
    tests, load this module without needing the layer.
    """
    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_config import LANGUAGE_MODELS, read_setting, resolve_model_id

    # Run within the section's Model inference geography.
    value = resolve_model_id(
        read_setting(LANGUAGE_MODELS.name, "summaryModelId"),
        region=os.environ.get("AWS_REGION", ""),
    )
    return (value or "").strip()


def _build_model_arn(model_id: str, region: str, account_id: str) -> str:
    """The inference-profile or foundation-model ARN of ``model_id``.

    The configuration layer's ``model_arn``, which knows the profile prefixes
    from the shipped catalogue; imported here for the reason
    :func:`_configured_model_id` gives.
    """
    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_config import model_arn

    return model_arn(model_id, region, account_id)


def execute(
    query: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_results: int = 10,
    user_id: str = None,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """
    Search meetings using Bedrock Knowledge Base with S3 Vectors.
    Enforces user-based access control.

    Args:
        query: Natural language search query
        start_date: Optional ISO 8601 date or date and time; meetings created from then
        end_date: Optional ISO 8601 date or date and time; meetings created until then
            (a date alone includes the whole day, in UTC)
        max_results: Maximum number of results (default: 10)
        user_id: User ID for access control
        is_admin: Whether user is admin (can see all meetings)

    Returns:
        Dict with answer and citations
    """
    if not query:
        raise ValueError("Query is required")

    kb_id = os.environ.get("TRANSCRIPT_KB_ID")

    if not kb_id:
        raise ValueError("Transcript Knowledge Base not configured")

    # Resolve the model ARN. Prefer the configured model (builds correct ARN type
    # for both foundation models and inference profiles). Fall back to the
    # legacy pre-built MODEL_ARN for backward compatibility.
    model_id = _configured_model_id()
    if model_id:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        account_id = (
            os.environ.get("AWS_ACCOUNT_ID") or boto3.client("sts").get_caller_identity()["Account"]
        )
        model_arn = _build_model_arn(model_id, region, account_id)
    else:
        model_arn = os.environ.get("MODEL_ARN")

    if not model_arn:
        raise ValueError(
            "Bedrock model not configured: choose one in the Amazon Bedrock Models section of the Configuration page"
        )

    kb_client = boto3.client("bedrock-agent-runtime")

    # Build metadata filters for UBAC and date range
    filters = []

    # UBAC: Non-admin users see only their meetings. The key is the one the summary
    # step writes into each document's metadata (getKBMetadata in bedrock_summary_lambda).
    if not is_admin and user_id:
        filters.append({"equals": {"key": OWNER_METADATA_KEY, "value": user_id}})

    # Date range filters, on when each meeting was created. A document written
    # before the summary step wrote this key has none, so it is not found by a
    # search given either date.
    if start_date:
        filters.append(
            {
                "greaterThanOrEquals": {
                    "key": CREATED_AT_METADATA_KEY,
                    "value": _date_bound(start_date, "startDate", end_of_day=False),
                }
            }
        )

    if end_date:
        filters.append(
            {
                "lessThanOrEquals": {
                    "key": CREATED_AT_METADATA_KEY,
                    "value": _date_bound(end_date, "endDate", end_of_day=True),
                }
            }
        )

    # Build retrieval configuration
    retrieval_config = {"vectorSearchConfiguration": {"numberOfResults": max_results}}

    # A RetrievalFilter's andAll takes two conditions or more, so one is sent bare.
    if len(filters) == 1:
        retrieval_config["vectorSearchConfiguration"]["filter"] = filters[0]
    elif filters:
        retrieval_config["vectorSearchConfiguration"]["filter"] = {"andAll": filters}

    # In the function for the reason given in _configured_model_id's docstring.
    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_config import knowledge_base_prompt_templates

    # Query Bedrock Knowledge Base
    try:
        response = kb_client.retrieve_and_generate(
            input={"text": query},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kb_id,
                    "modelArn": model_arn,
                    **knowledge_base_prompt_templates(),
                    "retrievalConfiguration": retrieval_config,
                },
            },
        )

        # Format response
        result = {
            "query": query,
            "answer": response.get("output", {}).get("text", "No answer generated"),
            "citations": [],
        }

        # Extract citations with meeting references
        for citation in response.get("citations", []):
            for ref in citation.get("retrievedReferences", []):
                location = ref.get("location", {}).get("s3Location", {})
                uri = location.get("uri", "")

                # Extract meeting ID from S3 URI
                # Format: s3://bucket/lma-transcripts/{meeting-id}/transcript.json
                meeting_id = None
                if "/lma-transcripts/" in uri:
                    parts = uri.split("/lma-transcripts/")
                    if len(parts) > 1:
                        meeting_id = parts[1].split("/")[0]

                result["citations"].append(
                    {
                        "meetingId": meeting_id,
                        "excerpt": ref.get("content", {}).get("text", ""),
                        "score": ref.get("metadata", {}).get("score", 0),
                        "uri": uri,
                        "meetingUrl": get_meeting_url(meeting_id),
                    }
                )

        logger.info(f"Search returned {len(result['citations'])} citations")
        return result

    except Exception as e:
        logger.error(f"Error querying Knowledge Base: {e}")
        raise ValueError(f"Knowledge Base query failed: {str(e)}")
