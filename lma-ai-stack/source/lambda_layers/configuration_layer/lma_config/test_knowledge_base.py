# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The RetrieveAndGenerate prompt templates carry the placeholders Bedrock fills in."""

from __future__ import annotations

from lma_config import knowledge_base_prompt_templates


def test_both_prompts_are_supplied() -> None:
    templates = knowledge_base_prompt_templates()
    assert set(templates) == {"generationConfiguration", "orchestrationConfiguration"}


def test_generation_prompt_has_the_search_results_query_and_citation_format() -> None:
    text = knowledge_base_prompt_templates()["generationConfiguration"]["promptTemplate"][
        "textPromptTemplate"
    ]
    for placeholder in ("$search_results$", "$query$", "$output_format_instructions$"):
        assert placeholder in text


def test_orchestration_prompt_has_the_conversation_and_query() -> None:
    text = knowledge_base_prompt_templates()["orchestrationConfiguration"]["promptTemplate"][
        "textPromptTemplate"
    ]
    for placeholder in ("$conversation_history$", "$query$", "$output_format_instructions$"):
        assert placeholder in text
