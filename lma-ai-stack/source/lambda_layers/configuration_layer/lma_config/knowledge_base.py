# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Prompt templates for Amazon Bedrock Knowledge Base RetrieveAndGenerate calls.

RetrieveAndGenerate has built-in prompts only for some models. For the others,
including the Claude 5.5 models LMA now uses for summaries, it refuses the call
("Custom prompt templates must be provided for both Orchestration and
Generation when using the selected model") unless both prompts are supplied.
The Meetings Query Tool and the MCP ``search_meetings`` tool both query the
meeting transcripts knowledge base with the summary model, so both pass the
templates below. They are sent with every model, not only the ones that need
them, so the answer does not change shape when the model setting changes.

Placeholders are Amazon Bedrock's: ``$search_results$``, ``$query$`` and
``$output_format_instructions$`` (which carries the citation format) in the
generation prompt; ``$conversation_history$``, ``$query$`` and
``$output_format_instructions$`` in the orchestration prompt.
"""

from __future__ import annotations

from typing import Any, Dict

GENERATION_PROMPT = (
    "You answer questions about the user's meetings using only the search results below, "
    "which come from meeting transcripts and summaries. Name the meetings an answer comes "
    "from. If the search results do not contain the answer, say that you could not find it "
    "in the meetings, and do not guess.\n\n"
    "<search_results>\n$search_results$\n</search_results>\n\n"
    "$output_format_instructions$\n\n"
    "Question: $query$"
)

ORCHESTRATION_PROMPT = (
    "$conversation_history$\n\n"
    "Rewrite the user's latest question as a standalone search query over meeting "
    "transcripts and summaries, keeping names, dates and topics from the conversation.\n\n"
    "$output_format_instructions$\n\n"
    "Question: $query$"
)


def knowledge_base_prompt_templates() -> Dict[str, Any]:
    """The ``generationConfiguration`` and ``orchestrationConfiguration`` keys for a
    RetrieveAndGenerate ``knowledgeBaseConfiguration``."""
    return {
        "generationConfiguration": {"promptTemplate": {"textPromptTemplate": GENERATION_PROMPT}},
        "orchestrationConfiguration": {
            "promptTemplate": {"textPromptTemplate": ORCHESTRATION_PROMPT}
        },
    }
