#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Strands-based Meeting Assistant Lambda Function
Provides meeting assistance using AWS Strands SDK
Supports dynamic MCP server loading from Public Registry
"""

import json
import os
import boto3
from boto3.dynamodb.conditions import Key, Attr
from typing import Dict, Any, Optional
import logging
from datetime import datetime
from bedrock_compat import (
    MEETING_ASSIST_FALLBACK_MAX_TOKENS,
    accepts_temperature,
    build_inference_config,
    claude_major_version,
    extract_text,
)
import tavily_secret

# Import MCP server loader
try:
    from mcp_server_loader import load_account_mcp_servers
    MCP_LOADER_AVAILABLE = True
except ImportError as e:
    logging.warning(f"MCP server loader not available: {e}")
    MCP_LOADER_AVAILABLE = False

# Import thinking tool wrapper
try:
    from thinking_tool_wrapper import wrap_tool_with_thinking, get_next_sequence
    THINKING_WRAPPER_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Thinking tool wrapper not available: {e}")
    THINKING_WRAPPER_AVAILABLE = False

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')

# Get AppSync endpoint from environment
APPSYNC_GRAPHQL_URL = os.environ.get('APPSYNC_GRAPHQL_URL', '')
EVENT_API_HTTP_URL = os.environ.get('EVENT_API_HTTP_URL', '')
ENABLE_STREAMING = os.environ.get('ENABLE_STREAMING', 'false').lower() == 'true'

# Sampling temperature for the assistant's answers, on the models that accept one.
# Above zero because this path is conversational rather than extractive.
AGENT_TEMPERATURE = 0.3

# Fallback for KB_MODEL_ID, reached only by a deployment whose environment does not
# set it. Tracks the default of the MeetingAssistKnowledgeBaseModelID parameter.
DEFAULT_KB_MODEL_ID = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'

# Claude generation from which a model is assumed to require custom prompt
# templates. This is a default for ids nobody has checked, not the boundary: see
# kb_model_supports_retrieve_and_generate().
KB_CUSTOM_PROMPTS_ASSUMED_FROM_MAJOR = 5

# Models known to be refused by a live knowledge base that the generation default
# above does not already cover. Claude Opus 4.1 returns the same "custom prompt
# templates must be provided" ValidationException as the Claude 5.5 models, while
# Claude Opus 4.6 is accepted, so a generation number alone cannot decide this.
#
# The us. profile is the measured one (us-west-2, 2026-10-21; its inference
# profile exists in that account, so this is a refusal and not an availability
# problem). Its global. twin is listed with it by inference, since every earlier
# measurement found the two profile forms of one model behaving alike, and
# withholding a tool for an id LMA does not offer anywhere is the harmless
# direction to be wrong in.
KB_MODELS_REQUIRING_CUSTOM_PROMPTS = frozenset({
    'us.anthropic.claude-opus-4-1-20250805-v1:0',
    'global.anthropic.claude-opus-4-1-20250805-v1:0',
})

# Upper bound on Query pages for one meeting's transcript. A 1 MB page holds a
# great many segments, so this is far above any real meeting; it exists so that a
# table which keeps handing back a continuation key cannot spin this function for
# its whole timeout. Matches the limit the fetch_transcript Lambda applies to the
# same read.
MAX_TRANSCRIPT_PAGES = 100

# Optional Bedrock Guardrail, set from the BedrockGuardrailId / BedrockGuardrailVersion
# CloudFormation parameters. Both are empty by default.
BEDROCK_GUARDRAIL_ID = os.environ.get('BEDROCK_GUARDRAIL_ID', '').strip()
BEDROCK_GUARDRAIL_VERSION = os.environ.get('BEDROCK_GUARDRAIL_VERSION', '').strip()

# A guardrail id with no version is a half-configured deployment: the requests
# below are built without a guardrail and the IAM role is granted no guardrail
# permission, so say so once per cold start rather than failing closed in
# silence. BedrockGuardrailVersion defaults to DRAFT, so reaching this needs the
# parameter to have been explicitly blanked.
if BEDROCK_GUARDRAIL_ID and not BEDROCK_GUARDRAIL_VERSION:
    logger.warning(
        "BEDROCK_GUARDRAIL_ID is set to '%s' but BEDROCK_GUARDRAIL_VERSION is empty, "
        "so no guardrail will be applied. Set the BedrockGuardrailVersion parameter "
        "(for example DRAFT, or a published version number) to enable it.",
        BEDROCK_GUARDRAIL_ID,
    )


def get_guardrail_config() -> Dict[str, str]:
    """
    Return the Bedrock guardrail identifier/version, or an empty dict.

    A guardrail is only usable when both the id and the version are set, so an
    empty dict is returned unless both environment variables have a value. All
    callers spread the result into their model configuration, which means an
    empty dict leaves the request exactly as it was before the guardrail
    parameters were introduced.
    """
    if BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION:
        return {
            'guardrail_id': BEDROCK_GUARDRAIL_ID,
            'guardrail_version': BEDROCK_GUARDRAIL_VERSION,
        }
    return {}


def build_kb_model_arn(model_id: str, kb_region: str, kb_account_id: str) -> str:
    """The ``modelArn`` a RetrieveAndGenerate call generates its answer with.

    A bare foundation-model id is addressed in the model catalogue, which has no
    account in its ARN; anything else is an inference profile, which lives in the
    deployment's own account.
    """
    if model_id.startswith("anthropic"):
        return f"arn:aws:bedrock:{kb_region}::foundation-model/{model_id}"
    return f"arn:aws:bedrock:{kb_region}:{kb_account_id}:inference-profile/{model_id}"


def kb_model_supports_retrieve_and_generate(model_id: str) -> bool:
    """Whether a Bedrock Knowledge Base can generate an answer with ``model_id``.

    A Knowledge Base supplies its own orchestration and generation prompt
    templates, and asking RetrieveAndGenerate for a model those templates do not
    cover returns a ValidationException stating that custom prompt templates must
    be provided for both Orchestration and Generation. Which models those are is a
    property of the individual model: Claude Opus 4.1 is refused and Claude Opus
    4.6 is accepted, though both are Claude 4, and the five Claude 5.5 ids that
    were measured were refused in both their ``global.`` and their ``us.`` forms.
    The inference-profile prefix makes no difference either way.
    ``us.anthropic.claude-opus-5-5`` was not measured; it is covered by the
    generation rule below, not by a result.

    The agent's own model is unaffected and may be any of them: it reaches Bedrock
    through Converse, which carries no such requirement. That difference is why
    the knowledge-base model is a CloudFormation parameter of its own
    (MeetingAssistKnowledgeBaseModelID) rather than the agent's, and that
    parameter -- whose AllowedValues are the ids verified against a live knowledge
    base -- is what normally decides this. This function is the backstop for a
    value that did not come through it, such as a standalone deployment of the
    nested template.

    Two rules, in order: an id known to be refused is refused, and otherwise a
    Claude generation at or above ``KB_CUSTOM_PROMPTS_ASSUMED_FROM_MAJOR`` is
    assumed to be. The assumption is there because the two failure directions are
    not symmetrical, the same reasoning bedrock_compat.accepts_temperature sets
    out: treating a usable model as unusable costs one tool and is logged, while
    treating an unusable one as usable costs every call that tool makes. It is a
    default for ids nobody has checked and not a claim about where the boundary
    lies -- there is no generational boundary to claim. Anything that is not a
    Claude model keeps the knowledge base's defaults.
    """
    if model_id in KB_MODELS_REQUIRING_CUSTOM_PROMPTS:
        return False
    major = claude_major_version(model_id)
    if major is None:
        return True
    return major < KB_CUSTOM_PROMPTS_ASSUMED_FROM_MAJOR


def apply_kb_guardrail(kb_input: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attach the configured guardrail to a retrieve_and_generate request, in place.

    RetrieveAndGenerate names the guardrail fields differently from the Converse
    API: guardrailId/guardrailVersion rather than guardrailIdentifier/
    guardrailVersion, and it nests them under the knowledge base's
    generationConfiguration. The request is returned unmodified when no
    guardrail is configured, so every caller keeps its previous behaviour by
    default. None of the callers set generationConfiguration themselves.
    """
    guardrail_config = get_guardrail_config()
    if not guardrail_config:
        return kb_input

    kb_config = kb_input['retrieveAndGenerateConfiguration']['knowledgeBaseConfiguration']
    generation_config = kb_config.setdefault('generationConfiguration', {})
    generation_config['guardrailConfiguration'] = {
        'guardrailId': guardrail_config['guardrail_id'],
        'guardrailVersion': guardrail_config['guardrail_version'],
    }
    return kb_input

def new_appsync_client(url: str):
    """A fresh AppSync GraphQL client for ``url``.

    One seam for constructing a client, so that the publisher below is the only
    thing deciding how long one lives.
    """
    from asst_gql_client import AppsyncRequestsGqlClient
    return AppsyncRequestsGqlClient(url=url, fetch_schema_from_transport=False)


class AppsyncPublisher:
    """One AppSync connection, reused for the mutations of a single invocation.

    gql's ``Client.execute`` opens a transport session, runs the mutation and
    closes the session again, and the requests transport's session is where the
    TLS connection lives. Publishing a streamed answer token by token through
    ``Client.execute`` therefore costs one TLS handshake per token: measured
    against the deployed function's logs, 0.107s per streamed chunk, which on a
    median answer is of the order of ten seconds spent connecting. Holding one
    session open for the whole answer leaves a single handshake.

    **Scoped to one invocation, deliberately, rather than cached at module
    level.** AppSyncIAMAuthentication resolves credentials once, when the client
    is constructed, and signs every later request with that object. In the Lambda
    environment those come from botocore's environment-variable provider, which
    returns static credentials unless AWS_CREDENTIAL_EXPIRATION is set -- and
    Lambda does not set it -- so a client held at module level would go on signing
    with whatever was resolved at cold start. This function is kept warm by
    periodic warmup pings, so its execution environments are long-lived by design,
    which is exactly the case where that matters. A publisher per invocation
    always signs with the credentials the environment holds now, and still removes
    all but one of the handshakes, because one invocation is one answer.

    A failed mutation discards the session instead of keeping one whose state is
    unknown: the next publish opens a fresh one, so a single failure costs one
    reconnect rather than the remainder of the answer.
    """

    def __init__(self, url: str):
        self._url = url
        self._client = None
        self._session = None

    def _connect(self):
        if self._session is None:
            client = new_appsync_client(self._url)
            # connect_sync() hands back a session that stays open until it is
            # closed, unlike Client.execute(), which opens and closes one per call.
            self._session = client.connect_sync()
            self._client = client
        return self._session

    def execute(self, document, variables: Dict[str, Any]):
        """Run one mutation on the shared session, reconnecting if it is gone."""
        session = self._connect()
        try:
            return session.execute(document, variable_values=variables)
        except Exception:
            self.close()
            raise

    def close(self):
        """Release the session. Safe to call more than once, and after a failure."""
        client, self._client, self._session = self._client, None, None
        if client is None:
            return
        try:
            client.close_sync()
        except Exception as e:
            # Nothing is retried on the way out: the connection is being
            # abandoned either way, and raising here would mask whatever ended
            # the stream.
            logger.warning(f"Error closing AppSync client: {str(e)}")


# Parsed once per execution environment rather than per published token: the
# document is a constant, and gql re-parses the string on every gql() call.
_CHAT_TOKEN_MUTATION = None


def get_chat_token_mutation():
    """The addChatToken mutation document, parsed on first use."""
    global _CHAT_TOKEN_MUTATION
    if _CHAT_TOKEN_MUTATION is None:
        from gql import gql
        _CHAT_TOKEN_MUTATION = gql("""
        mutation AddChatToken($input: AddChatTokenInput!) {
            addChatToken(input: $input) {
                CallId
                MessageId
                Token
                IsComplete
                Sequence
                Timestamp
                ThinkingStep {
                    Type
                    Content
                    ToolName
                    ToolInput
                    ToolResult
                    Success
                }
            }
        }
        """)
    return _CHAT_TOKEN_MUTATION


def subscriber_message_id(transcript_segment_args: Dict[str, Any]) -> Optional[str]:
    """The MessageId a UI subscriber could be listening on, or None if there is none.

    ``onAddChatToken`` takes both a CallId and a MessageId and both are required,
    so a published token only ever reaches a subscriber that knows the MessageId
    in advance. Every caller that has one supplies it in ``transcript_segment_args``:
    the chat path generates it (``chat-<uuid>``) and returns it to the browser to
    subscribe with, and the wake-phrase path supplies the SegmentId it is about to
    write. The Virtual Participant's voice assistant invokes this function
    directly with no ``transcript_segment_args`` at all, waits for the whole
    RequestResponse payload and hands the single returned string to its speech
    model, so nothing is listening and nothing persists the tokens -- addChatToken
    resolves against a NONE data source that only broadcasts.

    Returning None for that caller is what lets the streaming publish be skipped
    where it has no audience. It is decided from the payload rather than from a
    guess about who sent it, and in the direction that fails safe: an unrecognised
    caller that does supply an id keeps its tokens published.
    """
    return (
        transcript_segment_args.get('MessageId')
        or transcript_segment_args.get('SegmentId')
        or None
    )


def send_thinking_step_async(call_id: str, message_id: str, thinking_step: Dict[str, Any], sequence: int):
    """Fire-and-forget: send a thinking step in a background thread to avoid blocking."""
    import threading
    t = threading.Thread(
        target=send_thinking_step_to_appsync,
        args=(call_id, message_id, thinking_step, sequence),
        daemon=True
    )
    t.start()


class ThinkingStepHookProvider:
    """Hook provider to track tool usage and send thinking steps to AppSync"""
    
    def __init__(self, call_id: str, message_id: str):
        self.call_id = call_id
        self.message_id = message_id
        self.sequence_counter = 100  # Start at 100 to avoid conflicts with manual sequences
    
    def register_hooks(self, registry):
        """Register hook callbacks for tool lifecycle events"""
        from strands.hooks.events import BeforeToolCallEvent, AfterToolCallEvent
        
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)
    
    def on_before_tool_call(self, event):
        """Called before a tool is executed"""
        try:
            logger.info(f"🔧 Hook: on_before_tool_call FIRED!")
            
            if not APPSYNC_GRAPHQL_URL:
                logger.warning("   APPSYNC_GRAPHQL_URL not configured, skipping thinking step")
                return
            
            # Get tool name - MCPAgentTool uses tool_name property, regular tools use name
            if event.selected_tool:
                tool_name = getattr(event.selected_tool, 'tool_name', None) or getattr(event.selected_tool, 'name', None) or 'unknown'
            else:
                tool_name = 'unknown'
            
            # Get tool input
            tool_input = {}
            if hasattr(event.tool_use, 'input'):
                tool_input = event.tool_use.input
            elif hasattr(event.tool_use, 'arguments'):
                tool_input = event.tool_use.arguments
            
            logger.info(f"   Tool name: {tool_name}")
            logger.info(f"   Tool input: {tool_input}")
            
            thinking_step = {
                'type': 'tool_use',
                'tool_name': tool_name,
                'tool_input': tool_input,
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(f"🔧 Hook: Sending thinking step for tool - {tool_name}")
            send_thinking_step_to_appsync(self.call_id, self.message_id, thinking_step, self.sequence_counter)
            self.sequence_counter += 1
            logger.info(f"✓ Hook: Thinking step sent successfully")
            
        except Exception as e:
            logger.error(f"❌ Error in on_before_tool_call hook: {str(e)}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")
    
    def on_after_tool_call(self, event):
        """Called after a tool is executed"""
        try:
            logger.info(f"✅ Hook: on_after_tool_call FIRED!")
            
            if not APPSYNC_GRAPHQL_URL:
                logger.warning("   APPSYNC_GRAPHQL_URL not configured, skipping thinking step")
                return
            
            # Get tool name - MCPAgentTool uses tool_name property, regular tools use name
            if event.selected_tool:
                tool_name = getattr(event.selected_tool, 'tool_name', None) or getattr(event.selected_tool, 'name', None) or 'unknown'
            else:
                tool_name = 'unknown'
            
            # Check if there was an exception
            is_error = getattr(event.result, 'is_error', False)
            
            # Get result content - try multiple possible attributes
            result_content = ''
            if hasattr(event.result, 'content'):
                result_content = str(event.result.content)[:500]
            elif hasattr(event.result, 'output'):
                result_content = str(event.result.output)[:500]
            elif hasattr(event.result, 'text'):
                result_content = str(event.result.text)[:500]
            else:
                result_content = str(event.result)[:500]
            
            logger.info(f"   Tool name: {tool_name}")
            logger.info(f"   Success: {not is_error}")
            logger.info(f"   Result (truncated): {result_content[:100]}")
            
            thinking_step = {
                'type': 'tool_result',
                'tool_name': tool_name,
                'result': result_content,
                'success': not is_error,
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(f"✅ Hook: Sending result thinking step for tool - {tool_name}")
            send_thinking_step_to_appsync(self.call_id, self.message_id, thinking_step, self.sequence_counter)
            self.sequence_counter += 1
            logger.info(f"✓ Hook: Result thinking step sent successfully")
            
        except Exception as e:
            logger.error(f"❌ Error in on_after_tool_call hook: {str(e)}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")


def create_document_search_tool(kb_id: str, kb_region: str, kb_account_id: str, kb_model_id: str,
                                call_id: str = None, message_id: str = None):
    """Factory function to create document search tool with closure.

    Returns None when ``kb_model_id`` cannot drive RetrieveAndGenerate, so the
    caller offers the agent no document_search tool at all. A tool that is offered
    but cannot answer is worse than an absent one: the agent spends a turn calling
    it and reads the failure back as though it were retrieved material.
    """
    if not kb_model_supports_retrieve_and_generate(kb_model_id):
        logger.error(
            "document_search is not being offered to the agent: knowledge-base model '%s' "
            "requires custom Orchestration and Generation prompt templates, which LMA does "
            "not supply. Set the MeetingAssistKnowledgeBaseModelID CloudFormation parameter "
            "to a model a knowledge base can generate with.",
            kb_model_id,
        )
        return None

    from strands import tool

    @tool
    def document_search(query: str) -> str:
        """Search company knowledge base for internal documents, policies, procedures, or reference materials.
        
        Use this when:
        - User asks about company-specific information
        - Need to reference internal documentation
        - Questions about products, services, policies
        
        Args:
            query: The search query for company documents
            
        Returns:
            Relevant information from company knowledge base
        """
        if not kb_id:
            return "Document retrieval not configured - no knowledge base available"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'document_search',
                    'tool_input': {'query': query},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            logger.info(f"Document search tool executing: {query}")

            # The knowledge base generates its answer with its own model, not the
            # agent's. See kb_model_supports_retrieve_and_generate().
            model_arn = build_kb_model_arn(kb_model_id, kb_region, kb_account_id)

            # Query knowledge base
            kb_input = {
                "input": {'text': query},
                "retrieveAndGenerateConfiguration": {
                    'knowledgeBaseConfiguration': {
                        'knowledgeBaseId': kb_id,
                        'modelArn': model_arn
                    },
                    'type': 'KNOWLEDGE_BASE'
                }
            }

            apply_kb_guardrail(kb_input)

            response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
            result = response.get("output", {}).get("text", "No results found in knowledge base")
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'document_search',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            logger.info(f"Document search result: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(
                "document_search failed: retrieve_and_generate on knowledge base %s with "
                "model '%s' raised %s: %s",
                kb_id, kb_model_id, type(e).__name__, e,
            )
            # Phrased so the agent cannot mistake it for retrieved material. The
            # previous wording began "Error retrieving documents" and was followed
            # by the exception text, which reads enough like content that the
            # model answered from it.
            return (
                "DOCUMENT_SEARCH_FAILED. No documents were retrieved and this text is not "
                "a search result -- it carries no knowledge base content. Tell the user "
                "that document search is unavailable right now instead of answering from "
                f"this. Cause: {type(e).__name__}: {e}"
            )

    return document_search


def create_recent_meetings_tool(dynamodb_table_name: str, user_email: str, call_id: str = None, message_id: str = None):
    """Factory function to create recent meetings list tool with closure"""
    from strands import tool
    from datetime import datetime, timedelta
    
    @tool
    def recent_meetings_list(limit: int = 10) -> str:
        """Get a chronological list of recent meetings sorted by date (most recent first).
        
        Use this when:
        - User asks about "last meeting", "most recent meeting", or "latest meeting"
        - Need to establish chronological context before semantic search
        - Want to list recent meetings in time order
        - Comparing current meeting to previous meetings
        
        Args:
            limit: Number of recent meetings to return (default 10, max 50)
            
        Returns:
            JSON list of recent meetings with CallId, date, and duration
        """
        if not dynamodb_table_name:
            return "Recent meetings list not configured - no DynamoDB table available"
        
        if not user_email:
            return "Recent meetings list requires user authentication"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'recent_meetings_list',
                    'tool_input': {'limit': limit},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            # Limit to reasonable range
            limit = min(max(1, limit), 50)
            
            logger.info(f"Recent meetings tool executing: limit={limit} for user: {user_email}")
            
            table = dynamodb.Table(dynamodb_table_name)
            
            # Query recent date shards (last 30 days) to find meetings
            # DynamoDB structure: PK = cls#YYYY-MM-DD#s#NN, SK = ts#timestamp#callid
            meetings = []
            shards_per_day = 6  # 4-hour shards
            
            # Start from today and go backwards
            current_date = datetime.utcnow()
            
            for days_back in range(30):  # Look back 30 days
                query_date = current_date - timedelta(days=days_back)
                date_str = query_date.strftime('%Y-%m-%d')
                
                # Query each shard for this date (in reverse order for efficiency)
                for shard in range(shards_per_day - 1, -1, -1):
                    pk = f"cls#{date_str}#s#{shard:02d}"
                    
                    try:
                        response = table.query(
                            KeyConditionExpression=Key('PK').eq(pk),
                            FilterExpression=Attr('Owner').eq(user_email),
                            ScanIndexForward=False  # Sort descending (most recent first)
                        )
                        
                        for item in response.get('Items', []):
                            meetings.append({
                                'CallId': item.get('CallId', 'Unknown'),
                                'Date': item.get('CreatedAt', 'Unknown'),
                                'Duration': f"{int(item.get('TotalConversationDurationMillis', 0) / 1000)}s",
                                'SK': item.get('SK', '')  # For sorting
                            })
                            
                            # Stop if we have enough
                            if len(meetings) >= limit:
                                break
                                
                    except Exception as e:
                        logger.warning(f"Error querying shard {pk}: {e}")
                        continue
                    
                    if len(meetings) >= limit:
                        break
                
                if len(meetings) >= limit:
                    break
            
            # Sort all collected meetings by SK (timestamp) descending
            meetings.sort(key=lambda x: x.get('SK', ''), reverse=True)
            meetings = meetings[:limit]
            
            # Remove SK from output (internal use only)
            for m in meetings:
                m.pop('SK', None)
            
            if not meetings:
                result = "No recent meetings found"
            else:
                result = json.dumps(meetings, indent=2)
                logger.info(f"Recent meetings returned {len(meetings)} meetings")
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'recent_meetings_list',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in recent meetings list: {str(e)}")
            error_msg = f"Error retrieving recent meetings: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'recent_meetings_list',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return recent_meetings_list


def create_meeting_history_tool(transcript_kb_id: str, kb_region: str, kb_account_id: str,
                                kb_model_id: str, user_email: str, call_id: str = None,
                                message_id: str = None):
    """Factory function to create meeting history tool with closure.

    Returns None when ``kb_model_id`` cannot drive RetrieveAndGenerate, so the
    agent is offered no meeting_history tool. The transcript knowledge base is
    created by default, which makes this the tool most often affected: offered but
    unable to answer, it was called on most interactions and its failure read back
    as though past meetings had been searched and found wanting.
    """
    if not kb_model_supports_retrieve_and_generate(kb_model_id):
        logger.error(
            "meeting_history is not being offered to the agent: knowledge-base model '%s' "
            "requires custom Orchestration and Generation prompt templates, which LMA does "
            "not supply. Set the MeetingAssistKnowledgeBaseModelID CloudFormation parameter "
            "to a model a knowledge base can generate with. The recent_meetings_list tool "
            "is unaffected, so chronological listing of past meetings still works.",
            kb_model_id,
        )
        return None

    from strands import tool

    @tool
    def meeting_history(query: str, call_ids: str = "") -> str:
        """Search past meeting transcripts and summaries with semantic search and user-based access control.
        
        Use this when:
        - User asks about meetings on specific topics (semantic search)
        - Need detailed content from past meetings
        - Following up on recent_meetings_list with specific CallIds
        - Searching for action items, decisions, or discussions
        
        Args:
            query: The search query for past meetings
            call_ids: Optional comma-separated CallIds to limit search to specific meetings (use after recent_meetings_list)
            
        Returns:
            Detailed information from past meetings the user has access to
        """
        if not transcript_kb_id:
            return "Meeting history not configured - no transcript knowledge base available"
        
        if not user_email:
            return "Meeting history requires user authentication"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'meeting_history',
                    'tool_input': {'query': query, 'call_ids': call_ids},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            logger.info(f"Meeting history tool executing: {query} for user: {user_email}")
            logger.info(f"🔍 DEBUG: KB Configuration:")
            logger.info(f"   transcript_kb_id: {transcript_kb_id}")
            logger.info(f"   kb_region: {kb_region}")
            logger.info(f"   kb_model_id: {kb_model_id}")
            logger.info(f"   user_email (filter): {user_email}")

            # The knowledge base generates its answer with its own model, not the
            # agent's. See kb_model_supports_retrieve_and_generate().
            model_arn = build_kb_model_arn(kb_model_id, kb_region, kb_account_id)

            logger.info(f"   model_arn: {model_arn}")
            
            # Query with user-based access control filter
            kb_input = {
                "input": {'text': query},
                "retrieveAndGenerateConfiguration": {
                    'knowledgeBaseConfiguration': {
                        'knowledgeBaseId': transcript_kb_id,
                        'modelArn': model_arn,
                        "retrievalConfiguration": {
                            "vectorSearchConfiguration": {
                                "filter": {
                                    "equals": {
                                        "key": "Owner",
                                        "value": user_email
                                    }
                                }
                            }
                        }
                    },
                    'type': 'KNOWLEDGE_BASE'
                }
            }
            
            apply_kb_guardrail(kb_input)

            logger.info(f"🔍 DEBUG: KB Input: {json.dumps(kb_input, indent=2)}")

            response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
            
            logger.info(f"🔍 DEBUG: KB Response (full): {json.dumps(response, indent=2, default=str)}")
            
            result = response.get("output", {}).get("text", "No past meetings found matching your query")
            
            # Check if this is a guardrail response
            if "Sorry, I am unable to assist you with this request" in result:
                logger.error(f"❌ GUARDRAIL TRIGGERED! Response: {result}")
                logger.error(f"   This indicates the Knowledge Base has guardrails blocking the query")
                logger.error(f"   Query: {query}")
                logger.error(f"   User email filter: {user_email}")
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'meeting_history',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            logger.info(f"Meeting history result: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(
                "meeting_history failed: retrieve_and_generate on transcript knowledge base "
                "%s with model '%s' raised %s: %s",
                transcript_kb_id, kb_model_id, type(e).__name__, e,
            )
            # Phrased so the agent cannot mistake it for a search result. The
            # previous wording began "Error retrieving meeting history" and was
            # followed by the exception text, which reads enough like an answer
            # that the model summarised it as though past meetings had been read.
            error_msg = (
                "MEETING_HISTORY_SEARCH_FAILED. No past meetings were searched and this "
                "text is not a search result -- it carries no meeting content. Tell the "
                "user that past-meeting search is unavailable right now instead of "
                f"answering from this. Cause: {type(e).__name__}: {e}"
            )

            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'meeting_history',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return meeting_history


def create_current_meeting_transcript_tool(call_id: str, dynamodb_table_name: str, message_id: str = None):
    """Factory function to create current meeting transcript tool with closure"""
    from strands import tool
    
    @tool
    def current_meeting_transcript(lines: int = 20, mode: str = "recent") -> str:
        """Get transcript from the current meeting.
        
        Use this when:
        - User asks about what was said in THIS meeting
        - Need to reference recent statements or discussions
        - Summarizing, extracting action items, or analyzing current meeting
        - User asks about topics, decisions, or questions from current meeting
        - Request is ambiguous but likely meeting-related
        
        DO NOT use for:
        - General knowledge questions unrelated to the meeting
        - Questions about past meetings (use meeting_history instead)
        - Web searches or current events (use web_search instead)
        
        Args:
            lines: Number of recent transcript lines to return (default 20, max 100)
            mode: "recent" for last N lines, "full" for complete transcript, "semantic" for relevant excerpts
            
        Returns:
            Meeting transcript text formatted with speaker names
        """
        if not dynamodb_table_name:
            return "Current meeting transcript not available - no DynamoDB table configured"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'current_meeting_transcript',
                    'tool_input': {'lines': lines, 'mode': mode},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            # Limit to reasonable range
            lines = min(max(1, lines), 100)
            
            logger.info(f"Current meeting transcript tool executing: lines={lines}, mode={mode}")
            
            # Fetch full transcript
            full_transcript = fetch_meeting_transcript(call_id, dynamodb_table_name)
            
            if not full_transcript:
                result = "No transcript available for current meeting yet."
            elif mode == "full":
                result = full_transcript
            elif mode == "recent":
                # Return last N lines
                transcript_lines = full_transcript.split('\n')
                recent_lines = transcript_lines[-lines:] if len(transcript_lines) > lines else transcript_lines
                result = '\n'.join(recent_lines)
                logger.info(f"Returning {len(recent_lines)} recent transcript lines")
            else:
                # Default to recent
                transcript_lines = full_transcript.split('\n')
                recent_lines = transcript_lines[-lines:] if len(transcript_lines) > lines else transcript_lines
                result = '\n'.join(recent_lines)
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'current_meeting_transcript',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in current meeting transcript: {str(e)}")
            error_msg = f"Error retrieving current meeting transcript: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'current_meeting_transcript',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return current_meeting_transcript


def create_vnc_preview_control_tool(call_id: str, appsync_url: str, message_id: str = None):
    """Factory function to create VNC preview control tool"""
    from strands import tool
    from gql import gql
    from asst_gql_client import AppsyncRequestsGqlClient
    
    @tool
    def control_vnc_preview(action: str) -> str:
        """Control the VNC live view preview window on the meeting page.
        
        This tool allows you to show or hide the Virtual Participant's browser
        screen directly on the meeting page. The user can watch in real-time
        what the VP is doing.
        
        Use this when:
        - User asks to "show me what the bot is doing"
        - User wants to "see the virtual participant screen"
        - User asks to "open the live view" or "show live preview"
        - User wants to "close the preview" or "hide the screen"
        - User wants to monitor VP browser activity
        
        Args:
            action: Either "open" to show preview or "close" to hide it
            
        Returns:
            Success message confirming the action
            
        Examples:
            - User: "show me the virtual participant"
            - User: "open the live view"
            - User: "close the preview"
        """
        if action not in ['open', 'close']:
            return "Invalid action. Please use 'open' to show the preview or 'close' to hide it."
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'control_vnc_preview',
                    'tool_input': {'action': action},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            logger.info(f"VNC preview control: {action} for call {call_id}")
            
            # Initialize AppSync client
            appsync_client = AppsyncRequestsGqlClient(
                url=appsync_url,
                fetch_schema_from_transport=False
            )
            
            # GraphQL mutation
            mutation = gql("""
            mutation ToggleVNCPreview($input: ToggleVNCPreviewInput!) {
                toggleVNCPreview(input: $input) {
                    CallId
                    Action
                    Success
                    Timestamp
                }
            }
            """)
            
            variables = {
                'input': {
                    'CallId': call_id,
                    'Show': action == 'open'
                }
            }
            
            result = appsync_client.execute(mutation, variable_values=variables)
            
            if result.get('toggleVNCPreview', {}).get('Success'):
                action_past = "opened" if action == "open" else "closed"
                success_msg = f"✓ VNC live preview {action_past} successfully. The user can now {'see' if action == 'open' else 'no longer see'} the Virtual Participant's browser screen on the meeting page."
                
                # Send tool result thinking step
                if call_id and message_id and APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'tool_result',
                        'tool_name': 'control_vnc_preview',
                        'result': success_msg,
                        'success': True,
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
                
                return success_msg
            else:
                error_msg = f"Failed to {action} VNC preview. Please try again."
                
                # Send error thinking step
                if call_id and message_id and APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'tool_result',
                        'tool_name': 'control_vnc_preview',
                        'result': error_msg,
                        'success': False,
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
                
                return error_msg
                
        except Exception as e:
            logger.error(f"Error controlling VNC preview: {str(e)}")
            error_msg = f"Error controlling VNC preview: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'control_vnc_preview',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return control_vnc_preview


def create_vp_browser_control_tool(call_id: str, event_api_http_url: str, message_id: str = None):
    """Factory function to create VP browser control tool using Event API HTTP endpoint"""
    from strands import tool
    import uuid
    from datetime import datetime
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import hashlib
    
    @tool
    def control_vp_browser(action: str, url: str = "") -> str:
        """Control the Virtual Participant's browser to perform web searches and research during meetings.
        
        🔧 DEBUG: Tool entry point - parameters will be logged
        
        This tool allows you to navigate to websites and take screenshots through the Virtual Participant.
        Perfect for visual research, fact-checking, and gathering information during meetings.
        
        **IMPORTANT:** Before using this tool, you MUST first use control_vnc_preview(action="open")
        to open the VNC live preview window so the user can see the browser activity.
        
        Use this when:
        - User asks to "search for X online" or "look up X"
        - User wants to "show me X website" or "open X"
        - Need to fact-check information with visual proof
        - User wants screenshots or visual evidence
        - Researching products, competitors, or references
        
        Available actions:
        - "open_url": Open a URL in a new foreground tab (requires url parameter)
        - "screenshot": Take a screenshot of current page
        
        Args:
            action: The browser action to perform
            url: URL to open (for open_url action)
            
        Returns:
            Result of the browser action
            
        Examples:
            - User: "search for AWS pricing"
              → control_vp_browser(action="open_url", url="https://aws.amazon.com/pricing")
            
            - User: "show me the competitor's website"
              → control_vp_browser(action="open_url", url="https://competitor.com")
            
            - User: "take a screenshot"
              → control_vp_browser(action="screenshot")
        """
        # Send tool use thinking step
        if call_id and message_id and APPSYNC_GRAPHQL_URL:
            from datetime import datetime
            thinking_step = {
                'type': 'tool_use',
                'tool_name': 'control_vp_browser',
                'tool_input': {'action': action, 'url': url},
                'timestamp': datetime.utcnow().isoformat()
            }
            send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
        
        logger.info("=" * 80)
        logger.info("🔧 VP BROWSER CONTROL TOOL CALLED")
        logger.info(f"  Action: {action}")
        logger.info(f"  URL: {url}")
        logger.info(f"  CallId: {call_id}")
        logger.info(f"  Event API HTTP URL configured: {bool(event_api_http_url)}")
        logger.info(f"  Event API HTTP URL value: {event_api_http_url}")
        logger.info("=" * 80)
        
        valid_actions = ['open_url', 'screenshot']
        
        if action not in valid_actions:
            logger.error(f"❌ Invalid action: {action}")
            return f"Invalid action. Please use one of: {', '.join(valid_actions)}"
        
        # Validate required parameters
        if action == 'open_url' and not url:
            logger.error("❌ Missing URL for open_url action")
            return "URL parameter is required for open_url action"
        
        if not event_api_http_url:
            logger.error("❌ Event API HTTP URL not configured!")
            logger.error(f"   EVENT_API_HTTP_URL env var: {os.environ.get('EVENT_API_HTTP_URL', 'NOT SET')}")
            return "Event API not configured - VP browser control unavailable"
        
        logger.info("✓ All validations passed, proceeding with Event API publish")
        
        try:
            logger.info(f"🚀 Starting VP browser control: {action} for call {call_id}")
            
            # For open_url, we need to use new_page with url parameter
            # According to chrome-devtools-mcp docs, new_page can accept a url parameter
            # which will create a new page AND navigate to that URL
            
            if action == 'open_url':
                command_id = str(uuid.uuid4())
                timestamp = datetime.utcnow().isoformat() + 'Z'
                
                # Use new_page with url parameter to create new page and navigate
                event_payload = {
                    'commandId': command_id,
                    'CallId': call_id,
                    'toolName': 'new_page',
                    'arguments': json.dumps({
                        'url': url  # new_page accepts url parameter to navigate immediately
                    }),
                    'requestedBy': 'strands-agent',
                    'timestamp': timestamp,
                    'status': 'pending'
                }
                
                call_id_hash = hashlib.sha256(call_id.encode()).hexdigest()[:16]
                channel_name = f'/mcp-commands/{call_id_hash}'
                
                logger.info(f"Publishing MCP new_page command with URL to Event API")
                logger.info(f"  Original CallId: {call_id}")
                logger.info(f"  Channel hash: {call_id_hash}")
                logger.info(f"  Channel: {channel_name}")
                logger.info(f"  Event API HTTP URL: {event_api_http_url}")
                logger.info(f"  Command (new_page with url): {command_id} -> {url}")
                
                body = json.dumps({
                    'channel': channel_name,
                    'events': [json.dumps(event_payload)]
                })
                
                logger.info(f"  Request body: {body}")
                
                # Create AWS request for SigV4 signing
                request = AWSRequest(
                    method='POST',
                    url=f"{event_api_http_url}/event",
                    data=body,
                    headers={
                        'Content-Type': 'application/json',
                        'accept': 'application/json, text/javascript',
                        'content-encoding': 'amz-1.0'
                    }
                )
                
                # Sign request with SigV4
                credentials = boto3.Session().get_credentials()
                SigV4Auth(credentials, 'appsync', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(request)
                
                logger.info(f"  Sending HTTP POST to: {event_api_http_url}/event")
                
                # Send HTTP request
                response = requests.post(
                    f"{event_api_http_url}/event",
                    data=body,
                    headers=dict(request.headers),
                    timeout=(5, 30),
                )
                
                logger.info(f"📥 Response received:")
                logger.info(f"  Status: {response.status_code}")
                logger.info(f"  Headers: {dict(response.headers)}")
                logger.info(f"  Body: {response.text}")
                
                if response.status_code != 200:
                    logger.error(f"❌ Event API returned non-200 status: {response.status_code}")
                    logger.error(f"   Response: {response.text}")
                
                response.raise_for_status()
                
                logger.info("=" * 80)
                logger.info("✅ NEW_PAGE COMMAND PUBLISHED SUCCESSFULLY")
                logger.info(f"✅ Creating new page with URL: {url}")
                logger.info(f"✅ Command: {command_id}")
                logger.info("=" * 80)
                
                success_msg = f"✓ Opening {url} in new tab in Virtual Participant browser"
                
                # Send tool result thinking step
                if call_id and message_id and APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'tool_result',
                        'tool_name': 'control_vp_browser',
                        'result': success_msg,
                        'success': True,
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
                
                return success_msg
                
            elif action == 'screenshot':
                tool_name = 'take_screenshot'
                arguments = {'format': 'png', 'quality': 90}
            else:
                return f"Action {action} not yet implemented"
            
            # For screenshot, send single command
            command_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat() + 'Z'
            
            event_payload = {
                'commandId': command_id,
                'CallId': call_id,
                'toolName': tool_name,
                'arguments': json.dumps(arguments),
                'requestedBy': 'strands-agent',
                'timestamp': timestamp,
                'status': 'pending'
            }
            
            # Publish to Event API channel via HTTP
            # Use SHA256 hash of CallId (first 16 chars) for channel name
            call_id_hash = hashlib.sha256(call_id.encode()).hexdigest()[:16]
            channel_name = f'/mcp-commands/{call_id_hash}'
            
            logger.info(f"Publishing MCP command to Event API")
            logger.info(f"  Original CallId: {call_id}")
            logger.info(f"  Channel hash: {call_id_hash}")
            logger.info(f"  Channel: {channel_name}")
            logger.info(f"  Event API HTTP URL: {event_api_http_url}")
            logger.info(f"  Event payload: {json.dumps(event_payload)}")
            
            # Prepare HTTP request body
            body = json.dumps({
                'channel': channel_name,
                'events': [json.dumps(event_payload)]
            })
            
            logger.info(f"  Request body: {body}")
            
            # Create AWS request for SigV4 signing
            request = AWSRequest(
                method='POST',
                url=f"{event_api_http_url}/event",
                data=body,
                headers={
                    'Content-Type': 'application/json',
                    'accept': 'application/json, text/javascript',
                    'content-encoding': 'amz-1.0'
                }
            )
            
            # Sign request with SigV4
            credentials = boto3.Session().get_credentials()
            SigV4Auth(credentials, 'appsync', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(request)
            
            logger.info(f"  Sending HTTP POST to: {event_api_http_url}/event")
            
            # Send HTTP request
            response = requests.post(
                f"{event_api_http_url}/event",
                data=body,
                headers=dict(request.headers),
                timeout=(5, 30),
            )
            
            logger.info(f"📥 Response received:")
            logger.info(f"  Status: {response.status_code}")
            logger.info(f"  Headers: {dict(response.headers)}")
            logger.info(f"  Body: {response.text}")
            
            if response.status_code != 200:
                logger.error(f"❌ Event API returned non-200 status: {response.status_code}")
                logger.error(f"   Response: {response.text}")
            
            response.raise_for_status()
            
            logger.info("=" * 80)
            logger.info("✅ EVENT PUBLISHED SUCCESSFULLY TO EVENT API")
            logger.info(f"✅ MCP command sent: {command_id}")
            logger.info(f"✅ Channel: {channel_name}")
            logger.info(f"✅ Tool: {tool_name}")
            logger.info("=" * 80)
            
            if action == 'screenshot':
                success_msg = f"✓ Screenshot captured. Command ID: {command_id}"
            else:
                success_msg = f"✓ Browser action '{action}' completed. Command ID: {command_id}"
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'control_vp_browser',
                    'result': success_msg,
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return success_msg
                
        except Exception as e:
            logger.error("=" * 80)
            logger.error("❌ EXCEPTION IN VP BROWSER CONTROL")
            logger.error(f"   Exception type: {type(e).__name__}")
            logger.error(f"   Exception message: {str(e)}")
            logger.error(f"   Action attempted: {action}")
            logger.error(f"   CallId: {call_id}")
            logger.error("=" * 80)
            import traceback
            logger.error(f"   Full traceback:\n{traceback.format_exc()}")
            error_msg = f"Error controlling Virtual Participant browser: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'control_vp_browser',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return control_vp_browser


def send_chat_token_to_appsync(call_id: str, message_id: str, token: str, is_complete: bool,
                               sequence: int, publisher: Optional[AppsyncPublisher] = None):
    """
    Send a chat token to AppSync for real-time streaming

    ``publisher`` is the AppsyncPublisher holding the connection this answer's
    tokens go out on; the streaming loop creates one and passes it for every
    token. Without one a connection is opened and closed for this single token,
    which is what a caller publishing a single token on its own wants.
    """
    try:
        if not APPSYNC_GRAPHQL_URL:
            logger.warning("APPSYNC_GRAPHQL_URL not configured, skipping token streaming")
            return

        variables = {
            'input': {
                'CallId': call_id,
                'MessageId': message_id,
                'Token': token,
                'IsComplete': is_complete,
                'Sequence': sequence
            }
        }

        mutation = get_chat_token_mutation()
        if publisher is None:
            new_appsync_client(APPSYNC_GRAPHQL_URL).execute(mutation, variable_values=variables)
        else:
            publisher.execute(mutation, variables)
        logger.debug(f"Sent token {sequence} to AppSync: {token[:50]}...")

    except Exception as e:
        logger.error(f"Error sending token to AppSync: {str(e)}")


def send_thinking_step_to_appsync(call_id: str, message_id: str, thinking_step: Dict[str, Any], sequence: int):
    """
    Send a thinking step via the existing addChatToken mutation (piggyback on token stream)
    
    This reuses the existing subscription infrastructure instead of creating a separate one.
    The thinking step is embedded in the ChatToken's ThinkingStep field.
    
    Args:
        call_id: The call/meeting ID
        message_id: The message ID for this conversation
        thinking_step: Dict containing thinking step data
        sequence: Sequence number for ordering
    """
    try:
        if not APPSYNC_GRAPHQL_URL:
            logger.warning("APPSYNC_GRAPHQL_URL not configured, skipping thinking step streaming")
            return
        
        # A connection of its own, not the streaming publisher's: these are sent
        # from tool bodies and from background threads (send_thinking_step_async),
        # and a requests session is not safe to share across threads. There are a
        # few of these per answer rather than one per token, so they are not what
        # the measured per-chunk cost was about.
        appsync_client = new_appsync_client(APPSYNC_GRAPHQL_URL)

        # The same addChatToken mutation the tokens use, with an empty token and
        # the thinking step attached.
        mutation = get_chat_token_mutation()

        # Create thinking step data
        thinking_step_data = {
            'Type': thinking_step.get('type', 'unknown'),
            'Content': thinking_step.get('content'),
            'ToolName': thinking_step.get('tool_name'),
            'ToolInput': json.dumps(thinking_step.get('tool_input', {})) if thinking_step.get('tool_input') else None,
            'ToolResult': thinking_step.get('result'),
            'Success': thinking_step.get('success', True)
        }
        
        variables = {
            'input': {
                'CallId': call_id,
                'MessageId': message_id,
                'Token': '',  # Empty token for thinking steps
                'IsComplete': False,
                'Sequence': sequence,
                'ThinkingStep': thinking_step_data
            }
        }
        
        # Execute mutation
        appsync_client.execute(mutation, variable_values=variables)
        logger.debug(f"Sent thinking step {sequence} ({thinking_step.get('type')}) via ChatToken")
        
    except Exception as e:
        logger.error(f"Error sending thinking step to AppSync: {str(e)}")

def fetch_meeting_transcript(call_id: str, dynamodb_table_name: str) -> str:
    """
    Fetch the meeting transcript from DynamoDB
    """
    try:
        table = dynamodb.Table(dynamodb_table_name)
        pk = f'trs#{call_id}'

        # Read every page, not just the first. DynamoDB caps a Query response at
        # 1 MB of items *read* and applies FilterExpression only afterwards, and
        # the partial segments excluded here are numerous, so they spend the
        # budget without appearing in the result: even a moderately long meeting
        # spans several pages. A single query therefore hands the agent the
        # opening stretch of the meeting as though it were the whole thing, with
        # nothing to say otherwise. Mirrors get_transcripts() in
        # lma-ai-stack/source/lambda_functions/fetch_transcript/index.py.
        query_args = {
            'KeyConditionExpression': Key('PK').eq(pk),
            'FilterExpression': (
                (Attr('Channel').eq('AGENT') | Attr('Channel').eq('CALLER') | Attr('Channel').eq('AGENT_ASSISTANT'))
                & Attr('IsPartial').eq(False)
            ),
        }
        items = []
        start_key = None
        pages = 0
        while pages < MAX_TRANSCRIPT_PAGES:
            response = table.query(
                **query_args,
                **({'ExclusiveStartKey': start_key} if start_key else {}),
            )
            items.extend(response.get('Items', []))
            pages += 1
            next_key = response.get('LastEvaluatedKey')
            if not next_key or next_key == start_key:
                # A key identical to the one just used would ask for the same page
                # again forever. Stop rather than spin.
                break
            start_key = next_key
        if pages >= MAX_TRANSCRIPT_PAGES:
            logger.warning(
                "Stopped reading the transcript for %s at the %d page limit; "
                "the transcript handed to the assistant may be incomplete",
                call_id, MAX_TRANSCRIPT_PAGES,
            )

        # Sort by EndTime and format transcript
        items = sorted(items, key=lambda x: x.get('EndTime', 0))

        transcript_parts = []
        for item in items:
            speaker = item.get('Speaker', 'Unknown')
            transcript = item.get('Transcript', '')
            channel = item.get('Channel', '')
            
            # Format based on channel
            if channel == 'AGENT_ASSISTANT':
                transcript_parts.append(f"MeetingAssistant: {transcript}")
            else:
                transcript_parts.append(f"{speaker}: {transcript}")
        
        full_transcript = '\n'.join(transcript_parts)
        logger.info(
            f"Fetched transcript for {call_id}: {len(full_transcript)} characters "
            f"from {len(items)} segments over {pages} page(s)"
        )
        
        return full_transcript
        
    except Exception as e:
        logger.error(f"Error fetching transcript: {str(e)}")
        return ""

def query_knowledge_base(user_input: str, call_id: str) -> str:
    """
    Query Bedrock Knowledge Base for relevant context

    Nothing in this module calls this today; the per-meeting context it fetches is
    reached through the current_meeting_transcript tool instead. It is kept on the
    knowledge-base model for the same reason the two tools are, so that re-enabling
    it cannot reintroduce the coupling to the agent's model.
    """
    try:
        kb_id = os.environ.get('KB_ID')
        if not kb_id:
            return ""

        kb_model_id = os.environ.get('KB_MODEL_ID', DEFAULT_KB_MODEL_ID)
        kb_region = os.environ.get('KB_REGION', os.environ.get('AWS_REGION'))
        kb_account_id = os.environ.get('KB_ACCOUNT_ID')

        model_arn = build_kb_model_arn(kb_model_id, kb_region, kb_account_id)

        # Query knowledge base
        kb_input = {
            "input": {
                'text': user_input
            },
            "retrieveAndGenerateConfiguration": {
                'knowledgeBaseConfiguration': {
                    'knowledgeBaseId': kb_id,
                    'modelArn': model_arn,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "filter": {
                                "equals": {
                                    "key": "CallId",
                                    "value": call_id
                                }
                            }
                        }
                    }
                },
                'type': 'KNOWLEDGE_BASE'
            }
        }

        apply_kb_guardrail(kb_input)

        logger.info(f"Querying KB with input: {kb_input}")
        
        response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
        
        # Extract response text
        kb_response = response.get("output", {}).get("text", "")
        
        logger.info(f"KB response: {kb_response}")
        
        return kb_response
        
    except Exception as e:
        logger.error(f"Error querying knowledge base: {str(e)}")
        return ""

def handler(event, context):
    """
    Lambda handler for Strands-based meeting assistance with tools
    
    Expected event structure:
    {
        "transcript": "meeting transcript context",
        "userInput": "user question or request",
        "callId": "unique call identifier",
        "userEmail": "user@example.com"
    }
    """
    try:
        # ============================================================
        # Warmup handler: short-circuit for keep-alive pings
        # The orchestrator/ECS sends periodic warmup events to keep
        # the Lambda container warm and MCP connections alive.
        # This runs load_account_mcp_servers() which ensures all
        # MCP clients are started and healthy, then returns immediately
        # without calling Bedrock or doing any real work.
        # ============================================================
        if event.get('action') == 'warmup':
            import time
            warmup_start = time.time()
            logger.info("Warmup ping received - ensuring MCP clients are alive and tools are cached")
            mcp_count = 0
            tools_count = 0
            from_cache = False
            if MCP_LOADER_AVAILABLE:
                try:
                    mcp_clients, from_cache = load_account_mcp_servers()
                    mcp_count = len(mcp_clients)
                    # Pre-load tool lists from each MCP server so the Agent constructor
                    # doesn't have to wait for list_tools_sync() on the first real request.
                    # This is fast (~0ms) if tools are already cached, or ~200ms per server if not.
                    for client in mcp_clients:
                        try:
                            if hasattr(client, '_loaded_tools') and client._loaded_tools is None:
                                from strands._async import run_async
                                async def _load():
                                    return await client.load_tools()
                                tools = run_async(_load)
                                tools_count += len(tools)
                                logger.info(f"Pre-loaded {len(tools)} tools from MCP client")
                            elif hasattr(client, '_loaded_tools') and client._loaded_tools is not None:
                                tools_count += len(client._loaded_tools)
                        except Exception as e:
                            logger.warning(f"Warmup: Failed to pre-load tools: {e}")
                except Exception as e:
                    logger.warning(f"Warmup: MCP loading failed: {e}")
            warmup_elapsed = time.time() - warmup_start
            logger.info(f"Warmup complete in {warmup_elapsed:.2f}s - {mcp_count} MCP clients, {tools_count} tools, from_cache={from_cache}")
            return {
                'status': 'warm',
                'mcp_clients': mcp_count,
                'tools_count': tools_count,
                'from_cache': from_cache,
                'warmup_time_ms': int(warmup_elapsed * 1000)
            }

        logger.info(f"Strands Meeting Assist - Processing event: {json.dumps(event)}")

        # Extract parameters from event - handle both 'text' and 'userInput' for compatibility
        user_input = event.get('userInput', '') or event.get('text', '')
        call_id = event.get('callId', '') or event.get('call_id', '')
        conversation_history = event.get('conversation_history', [])
        dynamodb_table_name = event.get('dynamodb_table_name', '')
        user_email = event.get('userEmail', '') or event.get('user_email', '')
        
        if not user_input:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'userInput or text is required'
                })
            }
        
        # Get environment variables for tools
        kb_id = os.environ.get('KB_ID', '')
        transcript_kb_id = os.environ.get('TRANSCRIPT_KB_ID', '')
        kb_region = os.environ.get('KB_REGION', os.environ.get('AWS_REGION'))
        kb_account_id = os.environ.get('KB_ACCOUNT_ID', '')
        model_id = os.environ.get('MODEL_ID', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
        # The model the knowledge bases generate with, which is deliberately
        # separate from the agent's: see kb_model_supports_retrieve_and_generate().
        kb_model_id = os.environ.get('KB_MODEL_ID', DEFAULT_KB_MODEL_ID)
        # Whether the web search tool is available is decided from the secret's
        # ARN alone, so a deployment without a Tavily API key makes no Secrets
        # Manager call. The key itself is read inside the tool, on the first
        # search this execution environment performs.
        web_search_configured = tavily_secret.is_configured()
        
        # Fetch meeting transcript from DynamoDB
        transcript = fetch_meeting_transcript(call_id, dynamodb_table_name) if dynamodb_table_name else event.get('transcript', '')
        
        # Get message ID early for tool wrappers and status updates. A caller that
        # supplies no id of its own has no subscriber either; see
        # subscriber_message_id(). The synthetic id is still used for logging and
        # for the thinking-step sequence counters.
        transcript_segment_args = event.get('transcript_segment_args', {})
        subscriber_id = subscriber_message_id(transcript_segment_args)
        message_id = subscriber_id or f"msg-{call_id}"

        # Initialize tools list (includes both regular tools and MCP clients)
        tools = []
        
        # Load installed MCP servers (account-level) - returns MCPClient objects
        # Uses module-level caching: on warm Lambda starts with unchanged config,
        # cached clients are returned almost instantly (no DynamoDB query, no subprocess spawn)
        if MCP_LOADER_AVAILABLE:
            try:
                logger.info("Loading installed MCP servers...")
                mcp_clients, from_cache = load_account_mcp_servers()
                tools.extend(mcp_clients)
                
                if from_cache:
                    logger.info(f"Using {len(mcp_clients)} cached MCP clients (warm start)")
                    # Fire-and-forget status update - don't block the main path
                    if APPSYNC_GRAPHQL_URL and mcp_clients:
                        send_thinking_step_async(call_id, message_id, {
                            'type': 'status',
                            'content': f'Using {len(mcp_clients)} cached MCP servers',
                            'timestamp': datetime.utcnow().isoformat()
                        }, 0)
                else:
                    logger.info(f"Loaded {len(mcp_clients)} MCP clients from installed MCP servers (fresh)")
                    logger.info("Using Strands Managed Integration (experimental) - MCP clients will manage their own lifecycle")
                    # Fire-and-forget status update
                    if APPSYNC_GRAPHQL_URL:
                        send_thinking_step_async(call_id, message_id, {
                            'type': 'status',
                            'content': f'Loaded {len(mcp_clients)} MCP servers',
                            'timestamp': datetime.utcnow().isoformat()
                        }, 0)
            except Exception as e:
                logger.warning(f"Failed to load MCP servers: {e}")
                # Fire-and-forget error status
                if APPSYNC_GRAPHQL_URL:
                    send_thinking_step_async(call_id, message_id, {
                        'type': 'status',
                        'content': f'Failed to load MCP servers: {str(e)}',
                        'timestamp': datetime.utcnow().isoformat()
                    }, 0)
        
        # Add web search tool if an API key is configured (auto-enable)
        if web_search_configured:
            try:
                from tavily import TavilyClient
                from strands import tool

                # Capture call_id and message_id in closure for thinking steps
                _call_id = call_id
                _message_id = message_id
                
                @tool
                def web_search(query: str) -> str:
                    """Search the web for current information, news, facts, or data.
                    
                    Use this when:
                    - User asks about current events, prices, or statistics
                    - Information is not in knowledge bases
                    - Need real-time or recent information
                    
                    Args:
                        query: The search query string
                        
                    Returns:
                        Search results with titles, content, and source URLs
                    """
                    try:
                        # Send tool_use thinking step
                        if THINKING_WRAPPER_AVAILABLE and APPSYNC_GRAPHQL_URL:
                            from datetime import datetime
                            sequence = get_next_sequence(_message_id)
                            thinking_step = {
                                'type': 'tool_use',
                                'tool_name': 'web_search',
                                'tool_input': {'query': query},
                                'timestamp': datetime.utcnow().isoformat()
                            }
                            logger.info(f"🔧 Sending tool_use thinking step: web_search")
                            send_thinking_step_to_appsync(_call_id, _message_id, thinking_step, sequence)
                        
                        logger.info(f"Web search tool executing: {query}")

                        # Fetched from Secrets Manager on the first search and
                        # cached for the life of this execution environment.
                        api_key = tavily_secret.get_api_key()
                        if not api_key:
                            return (
                                "Web search is unavailable: the configured Tavily API key "
                                "could not be read. Answer from the other available tools."
                            )
                        tavily_client = TavilyClient(api_key=api_key)
                        response = tavily_client.search(query, max_results=3)
                        
                        # Format results
                        results = []
                        for result in response.get('results', []):
                            title = result.get('title', '')
                            content = result.get('content', '')
                            url = result.get('url', '')
                            results.append(f"{title}\n{content}\nSource: {url}")
                        
                        if not results:
                            formatted_results = "No web search results found"
                        else:
                            formatted_results = "\n\n".join(results)
                        
                        # Send tool_result thinking step
                        if THINKING_WRAPPER_AVAILABLE and APPSYNC_GRAPHQL_URL:
                            from datetime import datetime
                            sequence = get_next_sequence(_message_id)
                            thinking_step = {
                                'type': 'tool_result',
                                'tool_name': 'web_search',
                                'result': formatted_results[:500],  # Truncate
                                'success': True,
                                'timestamp': datetime.utcnow().isoformat()
                            }
                            logger.info(f"✓ Sending tool_result thinking step: web_search")
                            send_thinking_step_to_appsync(_call_id, _message_id, thinking_step, sequence)
                        
                        logger.info(f"Web search returned {len(results)} results")
                        return formatted_results
                        
                    except Exception as e:
                        # Send error thinking step
                        if THINKING_WRAPPER_AVAILABLE and APPSYNC_GRAPHQL_URL:
                            from datetime import datetime
                            sequence = get_next_sequence(_message_id)
                            thinking_step = {
                                'type': 'tool_result',
                                'tool_name': 'web_search',
                                'result': f"Error: {str(e)}",
                                'success': False,
                                'timestamp': datetime.utcnow().isoformat()
                            }
                            send_thinking_step_to_appsync(_call_id, _message_id, thinking_step, sequence)
                        
                        logger.error(f"Error in web search: {str(e)}")
                        return f"Error performing web search: {str(e)}"
                
                tools.append(web_search)
                logger.info("Web search tool enabled")
            except Exception as e:
                logger.warning(f"Could not enable web search tool: {str(e)}")
        
        # Add document retrieval tool if KB configured. The factory returns None
        # when the knowledge-base model cannot generate an answer, in which case
        # the agent is offered no such tool rather than one that cannot answer.
        if kb_id:
            document_search_tool = create_document_search_tool(
                kb_id=kb_id,
                kb_region=kb_region,
                kb_account_id=kb_account_id,
                kb_model_id=kb_model_id,
                call_id=call_id,
                message_id=message_id
            )
            if document_search_tool is not None:
                tools.append(document_search_tool)
                logger.info(f"Document retrieval tool enabled (KB model: {kb_model_id})")
        
        # Add recent meetings list tool if DynamoDB table and transcript KB configured
        if dynamodb_table_name and transcript_kb_id and user_email:
            tools.append(create_recent_meetings_tool(
                dynamodb_table_name=dynamodb_table_name,
                user_email=user_email,
                call_id=call_id,
                message_id=message_id
            ))
            logger.info("Recent meetings list tool enabled")
        
        # Add meeting history tool if transcript KB configured. As above, the
        # factory returns None when the knowledge-base model cannot generate.
        if transcript_kb_id and user_email:
            meeting_history_tool = create_meeting_history_tool(
                transcript_kb_id=transcript_kb_id,
                kb_region=kb_region,
                kb_account_id=kb_account_id,
                kb_model_id=kb_model_id,
                user_email=user_email,
                call_id=call_id,
                message_id=message_id
            )
            if meeting_history_tool is not None:
                tools.append(meeting_history_tool)
                logger.info(f"Meeting history tool enabled (KB model: {kb_model_id})")
        
        # Add current meeting transcript tool
        if dynamodb_table_name:
            tools.append(create_current_meeting_transcript_tool(
                call_id=call_id,
                dynamodb_table_name=dynamodb_table_name,
                message_id=message_id
            ))
            logger.info("Current meeting transcript tool enabled")
        
        # Add VNC preview control tool (available to all users)
        if APPSYNC_GRAPHQL_URL:
            tools.append(create_vnc_preview_control_tool(
                call_id=call_id,
                appsync_url=APPSYNC_GRAPHQL_URL,
                message_id=message_id
            ))
            logger.info("VNC preview control tool enabled")
        
        # Add VP browser control tool (available to all users)
        if EVENT_API_HTTP_URL:
            tools.append(create_vp_browser_control_tool(
                call_id=call_id,
                event_api_http_url=EVENT_API_HTTP_URL,
                message_id=message_id
            ))
            logger.info("VP browser control tool enabled (Event API HTTP)")
        
        # Initialize Strands Agent
        try:
            from strands import Agent
            from strands.models import BedrockModel
            
            # message_id and subscriber_id were resolved above, from the same event.

            logger.info(f"Using MessageId for streaming: {message_id}")
            logger.info(f"Tools enabled: {len(tools)}")
            
            # Configure Bedrock model with streaming
            # Note: Extended thinking mode not supported by all models
            #
            # guardrail_id / guardrail_version are the BedrockModel keyword
            # arguments the Strands Agents SDK uses to populate the Converse
            # API's guardrailConfig. They are only present when both
            # CloudFormation parameters were supplied; see get_guardrail_config().
            #
            # temperature is passed only to models that accept it. Strands puts it
            # straight into the Converse API's inferenceConfig, and the
            # reasoning-capable Claude generations reject a request that carries it,
            # so a model selected from the newer end of the
            # MeetingAssistServiceBedrockModelID list would otherwise fail every
            # turn. See bedrock_compat.accepts_temperature.
            guardrail_config = get_guardrail_config()
            model_kwargs = {
                'model_id': model_id,
                'streaming': ENABLE_STREAMING,
                **guardrail_config,
            }
            if accepts_temperature(model_id):
                model_kwargs['temperature'] = AGENT_TEMPERATURE
            bedrock_model = BedrockModel(**model_kwargs)
            if guardrail_config:
                logger.info(
                    f"Bedrock model configured: {model_id} "
                    f"(guardrail {BEDROCK_GUARDRAIL_ID} version {BEDROCK_GUARDRAIL_VERSION})"
                )
            else:
                logger.info(f"Bedrock model configured: {model_id} (no guardrail configured)")
            
            # Create hook provider for tracking tool usage
            thinking_hook = ThinkingStepHookProvider(call_id=call_id, message_id=message_id)
            logger.info(f"Created ThinkingStepHookProvider for call_id={call_id}, message_id={message_id}")
            
            # Create agent with tools (includes MCPClient objects for managed integration)
            # Per Strands docs: "The MCPClient implements the experimental ToolProvider interface,
            # enabling direct usage in the Agent constructor with automatic lifecycle management"
            # Note: Agent.__del__ calls remove_consumer on MCPClients, which stops them.
            # Cached MCPClients will be restarted on next invocation via load_tools() -> start().
            agent = Agent(
                model=bedrock_model,
                system_prompt=get_meeting_assistant_prompt_with_tools() if tools else get_meeting_assistant_prompt(),
                tools=tools if tools else None,
                hooks=[thinking_hook]  # Add hook provider to track all tool usage
            )
            
            logger.info(f"Agent created with {len(tools)} tools/clients and hook provider (managed integration)")
            logger.info(f"Hook provider registered: {thinking_hook}")
            
            # Fire-and-forget status update - don't block before Bedrock call
            if APPSYNC_GRAPHQL_URL:
                send_thinking_step_async(call_id, message_id, {
                    'type': 'status',
                    'content': f'Agent ready with {len(tools)} tools',
                    'timestamp': datetime.utcnow().isoformat()
                }, 1)
            
            # Prepare context for the agent with conversation history
            if conversation_history:
                # Format last 10 messages (5 pairs) for context
                recent_history = conversation_history[-10:]
                history_text = "\n\nPrevious conversation:\n"
                for msg in recent_history:
                    role = "User" if msg.get('role') == 'user' else "Assistant"
                    content = msg.get('content', '')
                    history_text += f"{role}: {content}\n"
                
                context_message = f"{history_text}\n\nCurrent User Request: {user_input}"
                logger.info(f"Including {len(recent_history)} messages from conversation history")
            else:
                context_message = f"User Request: {user_input}"
            
            # Handle streaming vs non-streaming. Tokens are only streamed when
            # some caller is listening for them: onAddChatToken requires the
            # MessageId, so a caller that supplied none cannot be subscribed, and
            # the mutation persists nothing. The Virtual Participant's voice
            # assistant is that caller -- it waits for the whole response payload
            # and speaks the single string this returns -- so it takes the
            # non-streaming path and the answer reaches it unchanged.
            if ENABLE_STREAMING and APPSYNC_GRAPHQL_URL and subscriber_id:
                logger.info("Streaming mode enabled - sending tokens to AppSync")

                # Import asyncio for async streaming
                import asyncio

                # One AppSync connection for every token of this answer, created
                # here so it can be closed whether the stream finishes or raises.
                # See AppsyncPublisher.
                token_publisher = AppsyncPublisher(APPSYNC_GRAPHQL_URL)

                async def stream_response():
                    from datetime import datetime
                    sequence = 0
                    thinking_sequence = 0
                    full_response = []

                    # Use stream_async to get async iterator
                    async for event in agent.stream_async(context_message):
                        logger.debug(f"Stream event received: {type(event)} - {event}")
                        
                        # Handle different event types from Strands SDK
                        if isinstance(event, dict):
                            event_type = event.get('type', 'unknown')
                            
                            # Reasoning/thinking events
                            if event_type == 'thinking' or event_type == 'reasoning':
                                thinking_step = {
                                    'type': 'reasoning',
                                    'content': event.get('content', event.get('text', '')),
                                    'timestamp': datetime.utcnow().isoformat()
                                }
                                send_thinking_step_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    thinking_step=thinking_step,
                                    sequence=thinking_sequence
                                )
                                thinking_sequence += 1
                            
                            # Tool use events
                            elif event_type == 'tool_use' or event_type == 'tool_call':
                                tool_name = event.get('name', event.get('tool_name', 'unknown'))
                                tool_input = event.get('input', event.get('parameters', {}))
                                thinking_step = {
                                    'type': 'tool_use',
                                    'tool_name': tool_name,
                                    'tool_input': tool_input,
                                    'timestamp': datetime.utcnow().isoformat()
                                }
                                send_thinking_step_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    thinking_step=thinking_step,
                                    sequence=thinking_sequence
                                )
                                thinking_sequence += 1
                                logger.info(f"Tool use detected: {tool_name}")
                            
                            # Tool result events
                            elif event_type == 'tool_result' or event_type == 'tool_response':
                                tool_name = event.get('name', event.get('tool_name', 'unknown'))
                                result = event.get('content', event.get('result', ''))
                                is_error = event.get('is_error', False)
                                thinking_step = {
                                    'type': 'tool_result',
                                    'tool_name': tool_name,
                                    'result': str(result)[:500],  # Truncate long results
                                    'success': not is_error,
                                    'timestamp': datetime.utcnow().isoformat()
                                }
                                send_thinking_step_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    thinking_step=thinking_step,
                                    sequence=thinking_sequence
                                )
                                thinking_sequence += 1
                                logger.info(f"Tool result received: {tool_name} - Success: {not is_error}")
                            
                            # Final response data tokens
                            elif 'data' in event:
                                token_text = event['data']
                                full_response.append(token_text)
                                
                                # Send token to AppSync
                                send_chat_token_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    token=token_text,
                                    is_complete=False,
                                    sequence=sequence,
                                    publisher=token_publisher
                                )
                                sequence += 1

                        # Handle string events (simple text tokens)
                        elif isinstance(event, str):
                            full_response.append(event)
                            send_chat_token_to_appsync(
                                call_id=call_id,
                                message_id=message_id,
                                token=event,
                                is_complete=False,
                                sequence=sequence,
                                publisher=token_publisher
                            )
                            sequence += 1

                    # Send completion token
                    send_chat_token_to_appsync(
                        call_id=call_id,
                        message_id=message_id,
                        token='',  # nosec B106 - Empty string is not a hardcoded password, it's a completion marker
                        is_complete=True,
                        sequence=sequence,
                        publisher=token_publisher
                    )

                    logger.info(f"Streaming complete. Response tokens: {sequence}, Thinking steps: {thinking_sequence}")
                    return ''.join(full_response)

                # Run the async streaming function
                try:
                    response_text = asyncio.run(stream_response())
                finally:
                    token_publisher.close()
                
            else:
                # Non-streaming mode (current behavior). Also the path taken when
                # streaming is configured but the caller supplied no MessageId, so
                # no subscription for these tokens can exist.
                if ENABLE_STREAMING and APPSYNC_GRAPHQL_URL and not subscriber_id:
                    logger.info(
                        "The caller supplied no MessageId, so no subscriber can be listening "
                        "for this answer's tokens; answering without publishing a token stream"
                    )
                response = agent(context_message)
                
                # Convert AgentResult to string if needed
                if hasattr(response, 'text'):
                    response_text = response.text
                elif hasattr(response, '__str__'):
                    response_text = str(response)
                else:
                    response_text = response
            
            logger.info(f"Strands agent response: {response_text}")
            
            # Format response for LMA
            return {
                'message': response_text,
                'callId': call_id,
                'source': 'strands_bedrock'
            }
            
        except ImportError as e:
            logger.error(f"Strands SDK not available: {e}")
            # Fallback to direct Bedrock call if Strands is not available
            return fallback_bedrock_response(transcript, user_input, call_id)
            
    except Exception as e:
        logger.error(f"Error in Strands meeting assist: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': f'Internal error: {str(e)}',
                'callId': call_id
            })
        }

def get_meeting_assistant_prompt() -> str:
    """
    Returns the system prompt for the meeting assistant
    """
    return """You are an AI assistant helping participants during a live meeting. Your role is to:

1. Answer questions based on the meeting context and transcript
2. Provide helpful information relevant to the discussion
3. Keep responses concise and focused (under 100 words when possible)
4. If you don't have enough context from the meeting transcript, use your general knowledge
5. Be professional and supportive

When responding:
- Reference specific parts of the meeting transcript when relevant
- Provide actionable insights when possible
- Ask clarifying questions if the request is ambiguous
- Maintain a helpful and professional tone"""


def get_meeting_assistant_prompt_with_tools() -> str:
    """
    Enhanced system prompt for agent with tools
    """
    return """You are an AI assistant helping participants during a live meeting. Your role is to:

1. Intelligently determine when meeting context is needed
2. Use available tools to provide accurate, up-to-date information
3. Keep responses concise and focused (under 100 words when possible)
4. Be professional and supportive

Tool usage guidelines:

**For VP browser control (IMPORTANT - TWO STEP PROCESS):**
When using control_vp_browser, you MUST ALWAYS:
1. FIRST call control_vnc_preview(action="open") to open the live preview window
2. THEN call control_vp_browser(action="open_url", url="...") to open the website

Example workflow:
- User: "show me apple.com"
  Step 1: control_vnc_preview(action="open")  ← REQUIRED FIRST
  Step 2: control_vp_browser(action="open_url", url="https://apple.com")

This ensures the user can see the browser activity in real-time.

**For current meeting queries (USE current_meeting_transcript tool):**
- Summarizing THIS meeting
- Extracting action items from THIS meeting
- Analyzing topics discussed in THIS meeting
- Fact-checking recent statements in THIS meeting
- Responding to questions about what was just said
- When request is ambiguous but likely meeting-related
- Default to using this tool for most meeting-related queries

Examples:
- "Summarize this meeting" → current_meeting_transcript(mode="full")
- "What were the action items?" → current_meeting_transcript(mode="full")
- "What did we just discuss?" → current_meeting_transcript(lines=20, mode="recent")
- "Fact check that statement" → current_meeting_transcript(lines=10, mode="recent")

**For past meetings queries:**
- "Last meeting" → recent_meetings_list(limit=1) → meeting_history with CallId
- "Meetings about X" → meeting_history(query="X")
- "Have we discussed this before?" → meeting_history(query="topic")

**For general queries (DO NOT use current_meeting_transcript):**
- General knowledge questions
- Current events, weather, prices → web_search
- Company policies, procedures → document_search
- Questions explicitly unrelated to the meeting

**Decision logic:**
1. If question mentions "this meeting", "current meeting", "just said", "action items", "summarize" → USE current_meeting_transcript
2. If question is about past meetings → USE meeting_history or recent_meetings_list
3. If question is general knowledge or current events → USE web_search or answer directly
4. If ambiguous → DEFAULT to using current_meeting_transcript (most queries are meeting-related)

Always maintain a helpful and professional tone."""

def fallback_bedrock_response(transcript: str, user_input: str, call_id: str) -> Dict[str, Any]:
    """
    Fallback function that uses direct Bedrock API calls if Strands is not available
    """
    try:
        bedrock_client = boto3.client('bedrock-runtime')
        model_id = os.environ.get('MODEL_ID', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
        
        # Prepare the prompt
        prompt = f"""You are an AI assistant helping during a meeting.

Meeting Context:
{transcript if transcript else "No meeting transcript available yet."}

User Request: {user_input}

Please provide a helpful response based on the meeting context. Keep it concise and professional."""

        # Prepare the message for Bedrock
        message = {
            "role": "user",
            "content": [{"text": prompt}]
        }
        
        # Apply the same optional guardrail as the Strands path, so the fallback
        # is not a way to reach the model without it. Empty unless both
        # CloudFormation parameters were supplied.
        # 'trace' matches what the Strands path sends: BedrockModel defaults
        # guardrail_trace to 'enabled', so setting it here keeps guardrail
        # intervention visible in the response on both paths rather than only on
        # the agent one.
        guardrail_config = get_guardrail_config()
        converse_kwargs = {}
        if guardrail_config:
            converse_kwargs['guardrailConfig'] = {
                'guardrailIdentifier': guardrail_config['guardrail_id'],
                'guardrailVersion': guardrail_config['guardrail_version'],
                'trace': 'enabled',
            }

        # Call Bedrock
        response = bedrock_client.converse(
            modelId=model_id,
            messages=[message],
            inferenceConfig=build_inference_config(
                model_id, MEETING_ASSIST_FALLBACK_MAX_TOKENS, AGENT_TEMPERATURE
            ),
            **converse_kwargs
        )

        # Extract response text, leaving out any reasoning block
        response_text = extract_text(response)
        
        logger.info(f"Fallback Bedrock response: {response_text}")
        
        return {
            'message': response_text,
            'callId': call_id,
            'source': 'bedrock_fallback'
        }
        
    except Exception as e:
        logger.error(f"Fallback Bedrock call failed: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': f'Fallback error: {str(e)}',
                'callId': call_id
            })
        }
