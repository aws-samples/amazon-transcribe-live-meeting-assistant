import boto3
import cfnresponse
import json
import os

def lambda_handler(event, context):
    print(event)
    the_event = event['RequestType']
    print("The event is: ", str(the_event))

    defaultPromptTableName = event['ResourceProperties']['LLMDefaultPromptTableName']
    customPromptTableName = event['ResourceProperties']['LLMCustomPromptTableName']

    llm_prompt_summary_template_file = os.environ['LAMBDA_TASK_ROOT'] + "/LLMPromptSummaryTemplate.json"
    llm_prompt_summary_template = open(llm_prompt_summary_template_file).read()

    response_data = {}
    dynamodb = boto3.resource('dynamodb')
    defaultPromptTable = dynamodb.Table(defaultPromptTableName)
    customPromptTable = dynamodb.Table(customPromptTableName)

    try:
        if the_event in ('Create', 'Update'):
            print("Populating / updating default prompt table (for Create or Update event):", defaultPromptTableName)
            summary_prompt_template_str = llm_prompt_summary_template
            try:
                summary_prompt_template = json.loads(summary_prompt_template_str)
            except Exception as e:
                print("Not a valid JSON:", str(e))
                summary_prompt_template = {"Summary": summary_prompt_template_str}

            update_expression = "SET"
            expression_attribute_names = {}
            expression_attribute_values = {}

            i = 1
            for key, value in summary_prompt_template.items():
                update_expression += f" #{i} = :{i},"
                expression_attribute_names[f"#{i}"] = f"{i}#{key}"
                expression_attribute_values[f":{i}"] = value
                i += 1

            update_expression = update_expression[:-1] # remove last comma

            response = defaultPromptTable.update_item(
                  Key={'LLMPromptTemplateId': 'LLMPromptSummaryTemplate'},
                  UpdateExpression=update_expression,
                  ExpressionAttributeValues=expression_attribute_values,
                  ExpressionAttributeNames=expression_attribute_names
                )
            print("DDB response", response)

        if the_event in ('Create'):
            print("Populating Custom Prompt table with default prompts (for Create event):", customPromptTableName)
            response = customPromptTable.update_item(
                  Key={'LLMPromptTemplateId': 'LLMPromptSummaryTemplate'},
                  UpdateExpression=update_expression,
                  ExpressionAttributeValues=expression_attribute_values,
                  ExpressionAttributeNames=expression_attribute_names
                )
            print("DDB response", response)

        # Everything OK... send the signal back
        print("Operation successful!")
        cfnresponse.send(event,
                         context,
                         cfnresponse.SUCCESS,
                         response_data)
    except Exception as e:
        print("Operation failed...")
        print(str(e))
        response_data['Data'] = str(e)
        cfnresponse.send(event,
                         context,
                         cfnresponse.FAILED,
                         response_data)