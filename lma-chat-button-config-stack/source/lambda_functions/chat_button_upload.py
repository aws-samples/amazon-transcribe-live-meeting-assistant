# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import boto3
import cfnresponse
import json
import os

DEFAULT_BUTTON_CONFIG_PK = "DefaultChatButtonConfig"
CUSTOM_BUTTON_CONFIG_PK = "CustomChatButtonConfig"
DEFAULT_BUTTON_CONFIG_INFO = """LMA default STRANDS agent chat button configuration, kept so a deployment can be rolled back to a release that reads this table.
The chat panel reads its buttons from the stack's configuration table. Edit them from the Edit Chat Buttons dialog on a meeting page; edits made here have no effect.
"""
CUSTOM_BUTTON_CONFIG_INFO = """LMA custom STRANDS agent chat button configuration as it was saved before the buttons moved into the stack's configuration table, which carries it across on upgrade.
The chat panel reads its buttons from the configuration table, so edit them from the Edit Chat Buttons dialog on a meeting page; edits made here have no effect.
Button keys are in the form 'N#Label' where N is a sequence number.
"""

def get_new_item(pk, info, button_config):
    item = {
        'ChatButtonConfigId': pk,
        '*Information*': info
    }
    i = 1
    for key, value in button_config.items():
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
            buttonConfigTableName = event['ResourceProperties']['ChatButtonConfigTableName']

            # Load the default button configuration
            chat_button_config_file = os.environ['LAMBDA_TASK_ROOT'] + "/ChatButtonConfig.json"
            chat_button_config = open(chat_button_config_file).read()
            
            dynamodb = boto3.resource('dynamodb')
            buttonTable = dynamodb.Table(buttonConfigTableName)
           
            print("Populating / updating default button config item (for Create or Update event):", buttonConfigTableName)
            button_config_str = chat_button_config
            button_config = json.loads(button_config_str)
            item = get_new_item(DEFAULT_BUTTON_CONFIG_PK, DEFAULT_BUTTON_CONFIG_INFO, button_config)
            print("Writing default item to DDB:", json.dumps(item))
            response = buttonTable.put_item(Item=item)
            print("DDB response", response)

            if the_event in ('Create'):
                print("Populating Custom Button Config table with empty config (for Create event):", buttonConfigTableName)
                item = get_new_item(CUSTOM_BUTTON_CONFIG_PK, CUSTOM_BUTTON_CONFIG_INFO, {})
                print("Writing initial custom item to DDB:", json.dumps(item))
                response = buttonTable.put_item(Item=item)
                print("DDB response", response)

    except Exception as e:
        print("Operation failed...")
        print(str(e))
        response_data['Data'] = str(e)
        cfn_status = cfnresponse.FAILED
    
    print("Returning CFN Status", cfn_status)
    cfnresponse.send(event, context, cfn_status, response_data)
