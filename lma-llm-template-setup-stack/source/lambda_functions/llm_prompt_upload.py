import boto3
import cfnresponse
import json
import os

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
            defaultPromptTableName = event['ResourceProperties']['LLMDefaultPromptTableName']
            customPromptTableName = event['ResourceProperties']['LLMCustomPromptTableName']

            llm_prompt_summary_template_file = os.environ['LAMBDA_TASK_ROOT'] + "/LLMPromptSummaryTemplate.json"
            llm_prompt_summary_template = open(llm_prompt_summary_template_file).read()
            dynamodb = boto3.resource('dynamodb')
            defaultPromptTable = dynamodb.Table(defaultPromptTableName)
           
            print("Populating / updating default prompt table (for Create or Update event):", defaultPromptTableName)
            prompt_templates_str = llm_prompt_summary_template
            prompt_templates = json.loads(prompt_templates_str)
            default_prompt_templates = {
                "*Information*": f"LMA default summary prompt templates. Do not edit - changes may be overridden by updates - override default prompts using table: {customPromptTableName}",
                **prompt_templates
            }
            update_expr, attr_names, attr_values = get_update_expr(default_prompt_templates)
            response = defaultPromptTable.update_item(
                  Key={'LLMPromptTemplateId': 'LLMPromptSummaryTemplate'},
                  UpdateExpression=update_expr,
                  ExpressionAttributeValues=attr_values,
                  ExpressionAttributeNames=attr_names
                )
            print("DDB response", response)

            if the_event in ('Create'):
                customPromptTable = dynamodb.Table(customPromptTableName)
                print("Populating Custom Prompt table with default prompts (for Create event):", customPromptTableName)
                custom_prompt_templates = {
                    "*Information*": f"LMA custom summary prompt templates. Prompt defined here override default prompts defined in table: {defaultPromptTableName}"
                }
                update_expr, attr_names, attr_values = get_update_expr(custom_prompt_templates)
                response = customPromptTable.update_item(
                    Key={'LLMPromptTemplateId': 'LLMPromptSummaryTemplate'},
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