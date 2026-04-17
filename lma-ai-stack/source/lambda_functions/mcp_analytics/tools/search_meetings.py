# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 1: search_lma_meetings
Semantic search across meeting transcripts using Bedrock Knowledge Base
"""

import logging
import os
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger()

# Bedrock inference profile prefixes (cross-region routing).
# Model IDs starting with these prefixes must use an inference-profile ARN,
# not a foundation-model ARN.
_INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")


def _build_model_arn(model_id: str, region: str, account_id: str) -> str:
    """Build the correct Bedrock model ARN based on the model ID prefix.

    Inference profiles (prefixed with region codes like ``us.``, ``eu.``,
    ``apac.``, or ``global.``) must be referenced via the
    ``inference-profile`` ARN path. Plain foundation model IDs use the
    ``foundation-model`` ARN path.
    """
    if any(model_id.startswith(p) for p in _INFERENCE_PROFILE_PREFIXES):
        return f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


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
        start_date: Optional ISO 8601 start date filter
        end_date: Optional ISO 8601 end date filter
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

    # Resolve the model ARN. Prefer BEDROCK_MODEL_ID (builds correct ARN type
    # for both foundation models and inference profiles). Fall back to the
    # legacy pre-built MODEL_ARN for backward compatibility.
    model_id = os.environ.get("BEDROCK_MODEL_ID", "").strip()
    if model_id:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        account_id = os.environ.get("AWS_ACCOUNT_ID") or boto3.client("sts").get_caller_identity()[
            "Account"
        ]
        model_arn = _build_model_arn(model_id, region, account_id)
    else:
        model_arn = os.environ.get("MODEL_ARN")

    if not model_arn:
        raise ValueError("Bedrock model not configured (set BEDROCK_MODEL_ID or MODEL_ARN)")

    kb_client = boto3.client("bedrock-agent-runtime")

    # Build metadata filters for UBAC and date range
    filters = []

    # UBAC: Non-admin users see only their meetings
    if not is_admin and user_id:
        filters.append({"equals": {"key": "owner", "value": user_id}})

    # Date range filters
    if start_date:
        filters.append({"greaterThanOrEquals": {"key": "meetingDate", "value": start_date}})

    if end_date:
        filters.append({"lessThanOrEquals": {"key": "meetingDate", "value": end_date}})

    # Build retrieval configuration
    retrieval_config = {"vectorSearchConfiguration": {"numberOfResults": max_results}}

    if filters:
        retrieval_config["vectorSearchConfiguration"]["filter"] = {"andAll": filters}

    # Query Bedrock Knowledge Base
    try:
        response = kb_client.retrieve_and_generate(
            input={"text": query},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kb_id,
                    "modelArn": model_arn,
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
                    }
                )

        logger.info(f"Search returned {len(result['citations'])} citations")
        return result

    except Exception as e:
        logger.error(f"Error querying Knowledge Base: {e}")
        raise ValueError(f"Knowledge Base query failed: {str(e)}")
