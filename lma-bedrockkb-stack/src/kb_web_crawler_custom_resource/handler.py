import boto3
import json
import cfnresponse

CLIENT = boto3.client('bedrock-agent')


def convert_numeric_strings(d):
    if isinstance(d, dict):
        for key, value in d.items():
            if isinstance(value, str):
                if value.isdigit():
                    d[key] = int(value)
                else:
                    try:
                        d[key] = float(value)
                    except ValueError:
                        pass
            elif isinstance(value, dict):
                convert_numeric_strings(value)
            elif isinstance(value, list):
                for item in value:
                    convert_numeric_strings(item)
    elif isinstance(d, list):
        for i in range(len(d)):
            d[i] = convert_numeric_strings(d[i])
    return d


def lambda_handler(event, context):
    print("Event: ", json.dumps(event))
    status = cfnresponse.SUCCESS
    physicalResourceId = event.get('PhysicalResourceId', None)
    responseData = {}
    reason = "Success"
    create_update_args = convert_numeric_strings(event['ResourceProperties'])
    create_update_args.pop('ServiceToken', None)
    if event['RequestType'] == 'Create':
        try:
            print(f"create_data_source args: {json.dumps(create_update_args)}")
            response = CLIENT.create_data_source(**create_update_args)
            print(f"create_data_source response: {response}")
            physicalResourceId = response['dataSource'].get(
                'dataSourceId', None)
        except Exception as e:
            status = cfnresponse.FAILED
            reason = f"Exception - {e}"
    elif event['RequestType'] == 'Update':
        try:
            create_update_args['dataSourceId'] = physicalResourceId
            print(f"update_data_source args: {json.dumps(create_update_args)}")
            response = CLIENT.update_data_source(**create_update_args)
            print(f"update_data_source response: {response}")
        except Exception as e:
            status = cfnresponse.FAILED
            reason = f"Exception - {e}"
    else:  # Delete
        try:
            delete_args = dict(
                dataSourceId=physicalResourceId,
                knowledgeBaseId=event['ResourceProperties']['knowledgeBaseId']
            )
            print(f"delete_data_source args: {json.dumps(delete_args)}")
            response = CLIENT.delete_data_source(**delete_args)
            print(f"delete_data_source response: {response}")
        except Exception as e:
            status = cfnresponse.FAILED
            reason = f"Exception - {e}"
    responseData = {'DataSourceId': physicalResourceId}
    print(f"Status: {status}, Reason: {reason}")
    cfnresponse.send(event, context, status, responseData,
                     physicalResourceId, reason=reason)
