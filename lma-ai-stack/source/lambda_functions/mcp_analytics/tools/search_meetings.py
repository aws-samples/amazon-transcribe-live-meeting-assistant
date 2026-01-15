# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 1: search_lma_meetings
Semantic search across meeting transcripts using Bedrock Knowledge Base
"""

import boto3
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger()


def execute(query: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
           max_results: int = 10, user_id: str = None, is_admin: bool = False) -> Dict[str, Any]:
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
    
    kb_id = os.environ.get('TRANSCRIPT_KB_ID')
    model_arn = os.environ.get('MODEL_ARN')
    
    if not kb_id:
        raise ValueError("Transcript Knowledge Base not configured")
    
    kb_client = boto3.client('bedrock-agent-runtime')
    
    # Build metadata filters for UBAC and date range
    filters = []
    
    # UBAC: Non-admin users see only their meetings
    if not is_admin and user_id:
        filters.append({
            "equals": {"key": "owner", "value": user_id}
        })
    
    # Date range filters
    if start_date:
        filters.append({
            "greaterThanOrEquals": {"key": "meetingDate", "value": start_date}
        })
    
    if end_date:
        filters.append({
            "lessThanOrEquals": {"key": "meetingDate", "value": end_date}
        })
    
    # Build retrieval configuration
    retrieval_config = {
        'vectorSearchConfiguration': {
            'numberOfResults': max_results
        }
    }
    
    if filters:
        retrieval_config['vectorSearchConfiguration']['filter'] = {'andAll': filters}
    
    # Query Bedrock Knowledge Base
    try:
        response = kb_client.retrieve_and_generate(
            input={'text': query},
            retrieveAndGenerateConfiguration={
                'type': 'KNOWLEDGE_BASE',
                'knowledgeBaseConfiguration': {
                    'knowledgeBaseId': kb_id,
                    'modelArn': model_arn,
                    'retrievalConfiguration': retrieval_config
                }
            }
        )
        
        # Format response
        result = {
            'query': query,
            'answer': response.get('output', {}).get('text', 'No answer generated'),
            'citations': []
        }
        
        # Extract citations with meeting references
        for citation in response.get('citations', []):
            for ref in citation.get('retrievedReferences', []):
                location = ref.get('location', {}).get('s3Location', {})
                uri = location.get('uri', '')
                
                # Extract meeting ID from S3 URI
                # Format: s3://bucket/lma-transcripts/{meeting-id}/transcript.json
                meeting_id = None
                if '/lma-transcripts/' in uri:
                    parts = uri.split('/lma-transcripts/')
                    if len(parts) > 1:
                        meeting_id = parts[1].split('/')[0]
                
                result['citations'].append({
                    'meetingId': meeting_id,
                    'excerpt': ref.get('content', {}).get('text', ''),
                    'score': ref.get('metadata', {}).get('score', 0),
                    'uri': uri
                })
        
        logger.info(f"Search returned {len(result['citations'])} citations")
        return result
    
    except Exception as e:
        logger.error(f"Error querying Knowledge Base: {e}")
        raise ValueError(f"Knowledge Base query failed: {str(e)}")