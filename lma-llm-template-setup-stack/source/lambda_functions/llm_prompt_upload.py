# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import boto3
import cfnresponse
import json
import os

DEFAULT_PROMPT_TEMPLATES_PK = "DefaultSummaryPromptTemplates"
CUSTOM_PROMPT_TEMPLATES_PK = "CustomSummaryPromptTemplates"
DEFAULT_PROMPT_TEMPLATES_INFO = f"""LMA default summary prompt templates. Do not edit - changes may be overridden by updates.
To override default prompts, use same keys in item: {CUSTOM_PROMPT_TEMPLATES_PK}. Prompt keys must be in the form 'N#Title' where N is a sequence number.
"""
CUSTOM_PROMPT_TEMPLATES_INFO = f"""LMA custom summary prompt templates. Any key values defined here override defaults with same key defined in item: {DEFAULT_PROMPT_TEMPLATES_PK}.
To disable a default value, override it here with the same key, and value either emplty or 'NONE'. Prompt keys must be in the form 'N#Title' where N is a sequence number.
"""

# The troubleshooting agent's system prompt. Stored here rather than in the
# agent's source because its wording is the whole feature: it carries LMA's
# error-pattern catalogue and the handful of facts a reader gets wrong about
# what the virtual participant persists, and both need correcting in the field
# without waiting on a 35-minute stack deployment.
#
# Flat named attributes rather than the 'N#Title' sequence used above: there is
# one prompt, it is read by key, and no ordering applies to it.
DEFAULT_TROUBLESHOOTING_PROMPT_PK = "DefaultTroubleshootingAgentPrompt"
CUSTOM_TROUBLESHOOTING_PROMPT_PK = "CustomTroubleshootingAgentPrompt"
TROUBLESHOOTING_PROMPT_FILE = "TroubleshootingAgentSystemPrompt.md"
DEFAULT_TROUBLESHOOTING_PROMPT_INFO = f"""LMA default troubleshooting agent system prompt. Do not edit - this item is rewritten on every stack update.
To override it, set a 'systemPrompt' attribute on item: {CUSTOM_TROUBLESHOOTING_PROMPT_PK}. That item is seeded once, on stack create, and is never rewritten.
"""
CUSTOM_TROUBLESHOOTING_PROMPT_INFO = f"""LMA custom troubleshooting agent system prompt. Set a 'systemPrompt' attribute here to replace the default in item: {DEFAULT_TROUBLESHOOTING_PROMPT_PK}.
The replacement takes effect on the next diagnosis - no redeployment is needed. Leave it empty, absent, or set to 'NONE' to use the default. Use <br> for a line break when editing in the DynamoDB console.
"""


def get_troubleshooting_prompt_item(pk, info, system_prompt=None):
    """Build a troubleshooting-prompt item.

    Omitting system_prompt builds the empty custom item, whose only job is to
    document the override in its own '*Information*' attribute so an operator
    finding the table knows where to put one.
    """
    item = {
        'LLMPromptTemplateId': pk,
        '*Information*': info
    }
    if system_prompt is not None:
        item['systemPrompt'] = system_prompt
    return item


def get_new_item(pk, info, prompt_templates):
    item = {
        'LLMPromptTemplateId': pk,
        '*Information*': info
    }
    i = 1
    for key, value in prompt_templates.items():
        # prepend sequence number to allow control of sort order later
        attr_name = f"{i}#{key}"
        item[attr_name] = value
        i += 1
    return item

def lambda_handler(event, context):
    print(event)
    the_event = event['RequestType']
    print("The event is: ", str(the_event))
    cfn_status = cfnresponse.SUCCESS
    response_data = {}
    try:
        if the_event in ('Create', 'Update'):
            promptTemplateTableName = event['ResourceProperties']['LLMPromptTemplateTableName']

            domain = os.environ.get('DOMAIN', '').lower()  # Get the "Domain" value from the environment variable

            # Load the appropriate template.json file based on the user input "Domain" value
            if domain.lower() == 'healthcare':
                llm_prompt_summary_template_file = os.environ[
                                                       'LAMBDA_TASK_ROOT'] + "/LLMPromptHealthcareSummaryTemplate.json"
            else:
                llm_prompt_summary_template_file = os.environ['LAMBDA_TASK_ROOT'] + "/LLMPromptSummaryTemplate.json"

            llm_prompt_summary_template = open(llm_prompt_summary_template_file).read()
            dynamodb = boto3.resource('dynamodb')
            promptTable = dynamodb.Table(promptTemplateTableName)
           
            print("Populating / updating default prompt item (for Create or Update event):", promptTemplateTableName)
            prompt_templates_str = llm_prompt_summary_template
            prompt_templates = json.loads(prompt_templates_str)
            item = get_new_item(DEFAULT_PROMPT_TEMPLATES_PK, DEFAULT_PROMPT_TEMPLATES_INFO, prompt_templates)
            print("Writing default item to DDB:", json.dumps(item))
            response = promptTable.put_item(Item=item)
            print("DDB response", response)

            if the_event in ('Create'):
                print("Populating Custom Prompt table with default prompts (for Create event):", promptTemplateTableName)
                item = get_new_item(CUSTOM_PROMPT_TEMPLATES_PK, CUSTOM_PROMPT_TEMPLATES_INFO, {})
                print("Writing initial custom item to DDB:", json.dumps(item))
                response = promptTable.put_item(Item=item)
                print("DDB response", response)

            # The troubleshooting agent's prompt. Written from a text file with
            # its newlines intact, not the '<br>' encoding the summary
            # templates use: it is 200+ lines and has to stay reviewable in a
            # pull request diff.
            troubleshooting_prompt_file = (
                os.environ['LAMBDA_TASK_ROOT'] + "/" + TROUBLESHOOTING_PROMPT_FILE
            )
            with open(troubleshooting_prompt_file) as f:
                troubleshooting_prompt = f.read()

            print("Populating / updating default troubleshooting prompt item:", promptTemplateTableName)
            item = get_troubleshooting_prompt_item(
                DEFAULT_TROUBLESHOOTING_PROMPT_PK,
                DEFAULT_TROUBLESHOOTING_PROMPT_INFO,
                troubleshooting_prompt,
            )
            response = promptTable.put_item(Item=item)
            print("DDB response", response)

            if the_event in ('Create'):
                print("Seeding empty custom troubleshooting prompt item (for Create event):", promptTemplateTableName)
                item = get_troubleshooting_prompt_item(
                    CUSTOM_TROUBLESHOOTING_PROMPT_PK, CUSTOM_TROUBLESHOOTING_PROMPT_INFO
                )
                print("Writing initial custom item to DDB:", json.dumps(item))
                response = promptTable.put_item(Item=item)
                print("DDB response", response)

    except Exception as e:
        print("Operation failed...")
        print(str(e))
        response_data['Data'] = str(e)
        cfn_status = cfnresponse.FAILED
    
    print("Returning CFN Status", cfn_status)
    cfnresponse.send(event,
                    context,
                    cfn_status,
                    response_data)