import boto3
import botocore
import cfnresponse
import json
import datetime
import time
import os
import re
from botocore.exceptions import ClientError

AWS_REGION = os.environ['AWS_REGION']
aws_account_id = ''
aws_partition = 'aws'

dt = datetime.datetime.utcnow()
cf = boto3.client('cloudformation')
ssm = boto3.client('ssm')
s3 = boto3.client('s3')
lam = boto3.client('lambda')
iam = boto3.client('iam')
cloudfront = boto3.client('cloudfront')


def propsChanged(props, oldprops, fields):
    for field in fields:
        if props.get(field) != oldprops.get(field):
            print(
                f"Prop {field} value changed. Old: {oldprops.get(field)}, New: {props.get(field)}")
            return True
    return False


def addBotToAistack(props, oldprops):
    asyncAgentAssistOrchestratorFunction = getStackResource(
        props["AISTACK"], "AsyncAgentAssistOrchestrator")
    response = lam.get_function_configuration(
        FunctionName=asyncAgentAssistOrchestratorFunction)
    envVars = response["Environment"]["Variables"]
    envVars["LEX_BOT_ID"] = props["LexMeetingAssistBotId"]
    envVars["LEX_BOT_ALIAS_ID"] = props["LexMeetingAssistAliasId"]
    envVars["LEX_BOT_LOCALE_ID"] = props["LexMeetingAssistLocaleId"]
    response = lam.update_function_configuration(
        FunctionName=asyncAgentAssistOrchestratorFunction,
        Environment={"Variables": envVars}
    )
    print("Updated AsyncAgentAssistOrchestratorFunction Environment variable to add Lex bot.")

    print("Updating updating Cognito Authenticated Role for Agent Assist...")
    agentAssistBotAuthRole = getStackResource(
        props["COGNITOSTACK"], "AgentAssistBotAuthRole")
    newArn = f'arn:{aws_partition}:lex:{AWS_REGION}:{aws_account_id}:bot-alias/{props["LexMeetingAssistBotId"]}/{props["LexMeetingAssistAliasId"]}'
    newPolicy = {'Version': '2012-10-17', 'Statement': [{'Action': [
        'lex:RecognizeText', 'lex:RecognizeUtterance', 'lex:DeleteSession', 'lex:PutSession'], 'Resource': newArn, 'Effect': 'Allow'}]}
    print('New Policy:')
    print(newPolicy)
    iam.put_role_policy(
        RoleName=agentAssistBotAuthRole,
        PolicyName='AgentAssistBotAthPolicy',
        PolicyDocument=json.dumps(newPolicy)
    )
    print("Done updating Cognito Authenticated Role for Agent Assist")

    # update config file and invalidate CF
    print("Updating lex-web-ui-loader-config.json...")
    webAppBucket = getStackResource(props["AISTACK"], "WebAppBucket")
    configKey = 'lex-web-ui-loader-config.json'
    configTemplateKey = 'lex-web-ui-loader-config-template.json'
    response = s3.get_object(Bucket=webAppBucket, Key=configTemplateKey)
    contents = response["Body"].read().decode("utf-8")
    contents = contents.replace(
        '${REACT_APP_LEX_BOT_ID}', props["LexMeetingAssistBotId"])
    contents = contents.replace(
        '${REACT_APP_LEX_BOT_ALIAS_ID}', props["LexMeetingAssistAliasId"])
    contents = contents.replace(
        '${REACT_APP_LEX_BOT_LOCALE_ID}', props["LexMeetingAssistLocaleId"])
    contents = contents.replace('${REACT_APP_AWS_REGION}', AWS_REGION)
    contents = contents.replace(
        '${REACT_APP_LEX_IDENTITY_POOL_ID}', props["LexMeetingAssistIdentityPoolId"])
    contents = contents.replace(
        '${REACT_APP_LEX_IDENTITY_POOL_CLIENT_ID}', props["LexMeetingAssistUserPoolClientId"])
    contents = contents.replace(
        '${REACT_APP_LEX_USER_POOL_NAME}', props["LexMeetingAssistUserPoolId"])
    contents = contents.replace(
        '${REACT_APP_LEX_DOMAIN_NAME}', props["LexMeetingAssistDomainName"])
    contents = contents.replace(
        '${CLOUDFRONT_DOMAIN}', props["CloudFrontDomainName"])
    print("New LexWebUI Config: ", json.dumps(contents))
    s3.put_object(Bucket=webAppBucket, Key=configKey, Body=contents)
    print("Done updating lex-web-ui-loader-config.json. Invalidating CloudFront...")

    cloudFrontDistro = getStackResource(
        props["AISTACK"], "WebAppCloudFrontDistribution")
    response = cloudfront.create_invalidation(
        DistributionId=cloudFrontDistro,
        InvalidationBatch={
            'Paths': {
                'Quantity': 1,
                'Items': [
                    '/lex-web-ui-loader-config.json'
                ]
            },
            'CallerReference': str(time.time()).replace(".", "")
        }
    )


def setupQnABot(props, oldprops):
    configureQnabotSettings(props)
    if propsChanged(props, oldprops, ["QNABOTSTACK", "QnaMeetingAssistDemoJson", "QNASummarizeCallFunction", "QNAMeetingAssistLambdaHookFunction"]):
        loadQnABotSamplePackage(props)
        buildQnABotLexBot(props)
    else:
        print("QnaBot demo data unchanged - skipping QnABot sample data update.")


def configureQnabotSettings(props):
    ssmParamName = getStackResource(
        props["QNABOTSTACK"], "DefaultQnABotSettings")
    value = ssm.get_parameter(Name=ssmParamName)
    settings = json.loads(value["Parameter"]["Value"])
    # modify settings
    # Set LLM params
    settings["LLM_QA_NO_HITS_REGEX"] = "Sorry,"
    # Additional settings provided by param QnaBotSettings
    additional_QnaBotSettings = json.loads(
        props.get("QnaBotSettings", "{}").replace('\n', ''))
    for k, v in additional_QnaBotSettings.items():
        settings[k] = v
    # save back to SSM
    response = ssm.put_parameter(
        Name=ssmParamName,
        Value=json.dumps(settings),
        Type='String',
        Overwrite=True
    )
    print(f"Updated SSM parameter: {ssmParamName}")


def loadQnABotSamplePackage(props):
    importBucket = getStackResource(props["QNABOTSTACK"], "ImportBucket")
    demoPath = props["QnaMeetingAssistDemoJson"]
    demoparts = demoPath.split('/', 1)
    demobucket = demoparts[0]
    demokey = demoparts[1]
    demoFile = os.path.basename(demoPath)
    demoFileTmp = f'/tmp/{demoFile}'
    print(f"Processing {demoFile} import...")
    # Download demo file from S3
    s3.download_file(demobucket, demokey, demoFileTmp)
    # Replace Summarize Lambda Hook placeholder with function ARN
    with open(demoFileTmp, 'r') as f:
        filedata = f.read()
    filedata = re.sub('<LCASummarizeCallFunctionName>',
                      props["QNASummarizeCallFunction"], filedata)
    filedata = re.sub('<QNAMeetingAssistLambdaHookFunctionName>',
                      props["QNAMeetingAssistLambdaHookFunction"], filedata)
    with open(demoFileTmp, 'w') as f:
        f.write(filedata)
    # Upload edited file to import bucket to trigger import
    statusFile = f'status/{demoFile}'
    s3.put_object(Bucket=importBucket,
                  Key=f'{statusFile}', Body='{"status":"Starting"}')
    s3.upload_file(demoFileTmp, importBucket, f'data/{demoFile}')
    print(f"...waiting for {demoFile} import to be complete...")
    status = "Starting"
    while status != "Complete":
        time.sleep(2)
        status = get_status(bucket=importBucket, statusFile=statusFile)
        print(f'Import Status: {status}')
        if status.startswith("FAILED"):
            raise ValueError(status)
    print("Import complete")


def buildQnABotLexBot(props):
    lexBuildLambdaStart = getStackResource(
        props["QNABOTSTACK"], "LexBuildLambdaStart")
    buildStatusBucket = getStackResource(
        props["QNABOTSTACK"], "BuildStatusBucket")
    statusFile = f'lexV2status.json'
    s3.put_object(Bucket=buildStatusBucket,
                  Key=f'{statusFile}', Body='{"status":"Starting"}')
    response = lam.invoke(FunctionName=lexBuildLambdaStart)
    status = "Starting"
    while status not in ["READY", "FAILED"]:
        time.sleep(5)
        status = get_status(bucket=buildStatusBucket, statusFile=statusFile)
        print(f'Bot Status: {status}')


def getStackResource(stackName, logicaResourceId):
    print(f"LogicalResourceId={logicaResourceId}")
    physicalResourceId = cf.describe_stack_resource(
        StackName=stackName,
        LogicalResourceId=logicaResourceId
    )["StackResourceDetail"]["PhysicalResourceId"]
    print(f"PhysicalResourceId={physicalResourceId}")
    return (physicalResourceId)


def get_status(bucket, statusFile):
    try:
        response = s3.get_object(
            Bucket=bucket, Key=statusFile, IfModifiedSince=dt)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("304", "NoSuchKey"):
            return f'{e.response["Error"]["Code"]} - {e.response["Error"]["Message"]}'
        else:
            raise e
    obj_status_details = json.loads(response["Body"].read().decode("utf-8"))
    return obj_status_details["status"]


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
        oldprops = event.get("OldResourceProperties", {})
        try:
            addBotToAistack(props, oldprops)
            if props["QNABOTSTACK"]:
                setupQnABot(props, oldprops)
        except Exception as e:
            print(e)
            reason = f"Exception thrown: {e}"
            status = cfnresponse.FAILED
    cfnresponse.send(event, context, status, responseData, reason=reason)
