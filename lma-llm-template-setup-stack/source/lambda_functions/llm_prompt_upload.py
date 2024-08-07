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