# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import boto3
import cfnresponse
import json
import os

AWS_REGION = os.environ['AWS_REGION']
aws_account_id = ''
aws_partition = 'aws'

cf = boto3.client('cloudformation')
lam = boto3.client('lambda')


def getStackResource(stackName, logicaResourceId):
    print(f"LogicalResourceId={logicaResourceId}")
    physicalResourceId = cf.describe_stack_resource(
        StackName=stackName,
        LogicalResourceId=logicaResourceId
    )["StackResourceDetail"]["PhysicalResourceId"]
    print(f"PhysicalResourceId={physicalResourceId}")
    return (physicalResourceId)


def configureStrandsMode(props):
    """Configure AsyncAgentAssistOrchestrator for STRANDS mode"""
    print("Configuring STRANDS mode - updating AsyncAgentAssistOrchestrator with STRANDS Lambda ARN")
    
    if "StrandsMeetingAssistFunction" in props:
        strands_function_name = props["StrandsMeetingAssistFunction"]
        print(f"Using STRANDS Lambda function name from parameter: {strands_function_name}")
        
        # Get the full ARN for the function
        try:
            response = lam.get_function(FunctionName=strands_function_name)
            strands_function_arn = response['Configuration']['FunctionArn']
            print(f"Found STRANDS Lambda ARN: {strands_function_arn}")
            
            # Update AsyncAgentAssistOrchestrator with the STRANDS Lambda ARN
            asyncAgentAssistOrchestratorFunction = getStackResource(
                props["AISTACK"], "AsyncAgentAssistOrchestrator")
            response = lam.get_function_configuration(
                FunctionName=asyncAgentAssistOrchestratorFunction)
            envVars = response["Environment"]["Variables"]
            envVars["LAMBDA_AGENT_ASSIST_FUNCTION_ARN"] = strands_function_arn
            
            # Add Transcript KB ID if available
            kbId = props.get("TranscriptBedrockKnowledgeBaseId", "")
            if kbId:
                print(f"Setting Transcript KB_ID: {kbId}")
            
            response = lam.update_function_configuration(
                FunctionName=asyncAgentAssistOrchestratorFunction,
                Environment={"Variables": envVars}
            )
            print("Updated AsyncAgentAssistOrchestrator with STRANDS Lambda ARN")

            # Update QueryKnowledgeBaseResolverFunction with Transcript KB ID
            queryKnowledgeBaseResolverFunction = getStackResource(
                props["AISTACK"], "QueryKnowledgeBaseResolverFunction")
            response = lam.get_function_configuration(
                FunctionName=queryKnowledgeBaseResolverFunction)
            envVars = response["Environment"]["Variables"]
            envVars["KB_ID"] = kbId if kbId else "Transcript KnowledgeBase is not enabled"
            response = lam.update_function_configuration(
                FunctionName=queryKnowledgeBaseResolverFunction,
                Environment={"Variables": envVars}
            )
            print(f"Updated QueryKnowledgeBaseResolverFunction Environment variable to add Transcript KB_ID: {kbId}")
            
        except Exception as e:
            print(f"Error configuring STRANDS mode: {e}")
            raise e
    else:
        raise ValueError("StrandsMeetingAssistFunction parameter not found - STRANDS mode requires the Strands function")


def handler(event, context):
    global aws_account_id
    global aws_partition
    aws_account_id = context.invoked_function_arn.split(":")[4]
    aws_partition = context.invoked_function_arn.split(":")[1]
    print(json.dumps(event))
    status = cfnresponse.SUCCESS
    reason = "Success"
    responseData = {}
    responseData['Data'] = "Success"
    if event['RequestType'] != 'Delete':
        props = event["ResourceProperties"]
        try:
            configureStrandsMode(props)
        except Exception as e:
            print(e)
            reason = f"Exception thrown: {e}"
            status = cfnresponse.FAILED
    cfnresponse.send(event, context, status, responseData, reason=reason)
