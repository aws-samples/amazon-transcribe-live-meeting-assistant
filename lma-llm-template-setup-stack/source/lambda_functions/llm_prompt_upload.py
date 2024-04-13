import boto3
import cfnresponse
import json
import os

DEFAULT_PROMPT_TEMPLATES_PK = "DefaultSummaryPromptTemplates"
CUSTOM_PROMPT_TEMPLATES_PK = "CustomSummaryPromptTemplates"

def get_update_expr(prompt_templates):
    update_expr = "SET"
    attr_names = {}
    attr_values = {}
    i = 0
    for key, value in prompt_templates.items():
        attr_name = key if key == "*Information*" else f"{i}#{key}"
        update_expr += f" #{i} = :{i},"
        attr_names[f"#{i}"] = attr_name
        attr_values[f":{i}"] = value
        i += 1
    update_expr = update_expr[:-1] # remove last comma
    return update_expr, attr_names, attr_values

def lambda_handler(event, context):
    print(event)
    the_event = event['RequestType']
    print("The event is: ", str(the_event))
    cfn_status = cfnresponse.SUCCESS
    response_data = {}
    try:
        if the_event in ('Create', 'Update'):
            promptTemplateTableName = event['ResourceProperties']['LLMPromptTemplateTableName']

            llm_prompt_summary_template_file = os.environ['LAMBDA_TASK_ROOT'] + "/LLMPromptSummaryTemplate.json"
            llm_prompt_summary_template = open(llm_prompt_summary_template_file).read()
            dynamodb = boto3.resource('dynamodb')
            promptTable = dynamodb.Table(promptTemplateTableName)
           
            print("Populating / updating default prompt item (for Create or Update event):", promptTemplateTableName)
            prompt_templates_str = llm_prompt_summary_template
            prompt_templates = json.loads(prompt_templates_str)
            default_prompt_templates = {
                "*Information*": f"LMA default summary prompt templates. Do not edit - changes may be overridden by updates - override default prompts using same keys in item: {CUSTOM_PROMPT_TEMPLATES_PK}",
                **prompt_templates
            }
            update_expr, attr_names, attr_values = get_update_expr(default_prompt_templates)
            response = promptTable.update_item(
                  Key={'LLMPromptTemplateId': DEFAULT_PROMPT_TEMPLATES_PK},
                  UpdateExpression=update_expr,
                  ExpressionAttributeValues=attr_values,
                  ExpressionAttributeNames=attr_names
                )
            print("DDB response", response)

            if the_event in ('Create'):
                print("Populating Custom Prompt table with default prompts (for Create event):", promptTemplateTableName)
                custom_prompt_templates = {
                    "*Information*": f"LMA custom summary prompt templates. Key values defined here override defaults with same key defined in item: {DEFAULT_PROMPT_TEMPLATES_PK}. To disable a default value, override here with the value 'NONE' for the same key."
                }
                update_expr, attr_names, attr_values = get_update_expr(custom_prompt_templates)
                response = promptTable.update_item(
                    Key={'LLMPromptTemplateId': CUSTOM_PROMPT_TEMPLATES_PK},
                    UpdateExpression=update_expr,
                    ExpressionAttributeValues=attr_values,
                    ExpressionAttributeNames=attr_names
                    )
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