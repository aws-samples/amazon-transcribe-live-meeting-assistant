# Developer README

The main README is here: [Live Meeting Assistant (LMA) with Amazon Transcribe, Amazon Q Business Expert, and Amazon Bedrock](./README.md)

This Developer README describes how to build the project from the source code - for developer types - so you can deploy to additional regions, or build and deploy customized source code.

### 1. Dependencies

To deploy or to publish, you need to have the following packages installed on your computer:

1. bash shell (Linux, MacOS, Windows-WSL)
2. node v18 and npm 
3. docker
4. zip
5. python3, pip3, virtualenv
6. aws (AWS CLI)
7. sam (AWS SAM)

Copy the GitHub repo to your computer. Either:
- use the git command: git clone https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant.git
- OR, download and expand the ZIP file from the GitHub page: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/archive/refs/heads/main.zip

## Publish the solution

In our main README, you will see that we provided Easy Deploy Buttons to launch a stack using pre-built templates that we published already to an S3 bucket. 

If you want to build and publish your own template, to your own S3 bucket, so that others can easily use a similar easy button approach to deploy a stack, using *your* templates, in your preferred region, here's how.

Navigate into the project root directory and, in a bash shell, run:

1. `./publish.sh <cfn_bucket_basename> <cfn_prefix> <region e.g. us-east-1>`.  
  This:
    - checks your system dependendencies for required packages (see Dependencies above)
    - creates CloudFormation templates and asset zip files
    - publishes the templates and required assets to an S3 bucket in your account called `cfn_bucket_basename-region` (it creates the bucket if it doesn't already exist)
    - optionally add a final parameter `public` if you want to make the templates public. Note: your bucket and account must be configured not to Block Public Access using new ACLs.

That's it! There's just one step.
  
When completed, it displays the CloudFormation templates S3 URLs and 1-click URLs for launching the stack creation in CloudFormation console, e.g.:
```
OUTPUTS
Template URL: https://s3.us-east-1.amazonaws.com/yourbucketbasename-us-east-1/lma-test/lma-main.yaml
CF Launch URL: https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://s3.us-east-1.amazonaws.com/yourbucketbasename-us-east-1/lma-test/lma-main.yaml&stackName=LMA
Done
``````

Follow the deployment directions in the main [README](./README.md), but use your own CF Launch URL instead of our pre-built templates (Launch Stack buttons). 

## Domain expansion
You can follow the steps below to extend the LMA solution to any particular business domain.

1. Open lma-main.yaml file and add the name under Domain parameter - 
``` 
     Domain:
        Default: ''
        Type: String
        AllowedValues:
            - ''
            - 'Healthcare'
```
2. Add a condition in the lma-main.yaml file to check if the domain you are adding is selected - 
```
IsYOURDomainSelected: !Equals [ !Ref Domain, 'DOMAIN_NAME' ]

```
3. If your domain is selected you can either deploy your own custom stacks or edit exiting ones. In this example we are showing how you can edit the existing stacks to add your own Prompts
In the lma-main.yaml file there is a MEETINGASSISTSETUP stack information. Go to the section and add the custom jsonl for your domain. 
The custom .jsonl should be added under lma-meetingassist-setup-stack if you want to have your own custom prompts for the meeting assist feature. 
For the healthcare domain we have added qna-ma-healthcare-demo.jsonl with custom prompts aligned with Healthcare domain.

```
        QnaMeetingAssistDemoJson: !If
          - IsHealthcareDomainSelected
          - <ARTIFACT_BUCKET_TOKEN>/<ARTIFACT_PREFIX_TOKEN>/lma-meetingassist-setup-stack/qna-ma-healthcare-demo.jsonl
          - <ARTIFACT_BUCKET_TOKEN>/<ARTIFACT_PREFIX_TOKEN>/lma-meetingassist-setup-stack/qna-ma-demo.jsonl
          
```
In this example above, the QnaMeetingAssistDemoJson property will be set to:
- qna-ma-healthcare-demo.jsonl if Healthcare Domain is selected(IsHealthcareDomainSelected)
- qna-ma-demo.jsonl if Healthcare domain is not selected. 
- You can add an else section with your domain name selected like - 
    ```
  Type: AWS::SomeService::SomeResource
    Properties:
      Name: !If
        - IsInsuranceDomainSelected
        - insurance-resource
        - !If
          - IsFinanceDomainSelected
          - finance-resource
          - !If
            - IsOther
            - other-resource
            - !Ref 'AWS::NoValue'
  ```
In this example, the Name property of MyResource will be set to:
- insurance-resource if the EnvironmentName parameter is Insurance (IsInsuranceDomainSelected condition is true)
- finance-resource if the EnvironmentName parameter is Finance (IsFinanceDomainSelected condition is true)
- other-resource if the EnvironmentName parameter is neither prod nor test (IsOther condition is true)
The property will be removed if none of the conditions are true (using AWS::NoValue)

4. The lma-llm-template-setup-stack contains the prompts for the summary, details, SOAP, BIRP notes for Healthcare domain. You may want to add your own *.json file with the prompts you need for your business domain
To do that add your own template.json file and go to llm_prompt_upload.py file
5. Edit the file and update the logic to add the domain under the function def lambda_handler(event, context)
```
# Load the appropriate template.json file based on the user input "Domain" value
if domain.lower() == 'healthcare':
    llm_prompt_summary_template_file = os.environ[
                                           'LAMBDA_TASK_ROOT'] + "/LLMPromptHealthcareSummaryTemplate.json"

if domain.lower() == 'YOURDOMAIN':
    llm_prompt_summary_template_file = os.environ[
                                           'LAMBDA_TASK_ROOT'] + "/LLMPromptYOURDOMAINTemplate.json"


else:
    llm_prompt_summary_template_file = os.environ['LAMBDA_TASK_ROOT'] + "/LLMPromptSummaryTemplate.json"


```

6. Finally ensure the qna-ma-yourdomain-demo.jsonl file is copied from local to the s3 bucket with the appropriate permission by adding it to the publish.sh
Open the publish.sh file and search for qna-ma-demo.jsonl. Add your domain file - qna-ma-yourdomain.jsonl
```
aws s3 cp ./qna-ma-yourdomain-demo.jsonl s3://${BUCKET}/${PREFIX_AND_VERSION}/lma-meetingassist-setup-stack/qna-ma-yourdomain-demo.jsonl

```
That's it. Now you can build the package and deploy the new LMA-YOURDOMAIN Solution and test it out. 

## Contributing, and reporting issues

We welcome your contributions to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

## Security

See [Security issue notifications](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](./LICENSE) file.