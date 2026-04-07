# Developer README

The main README is here: [Live Meeting Assistant (LMA) with Amazon Transcribe, Amazon Bedrock, and Strands Agents](./README.md)

This Developer README describes how to build the project from the source code - for developer types - so you can deploy to additional regions, or build and deploy customized source code.

### 1. Dependencies

To deploy or to publish, you need to have the following packages installed on your computer:

1. bash shell (Linux, MacOS, Windows-WSL)
2. node v18/v20/v22 and npm 
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

## Customizing the Meeting Assistant

The LMA meeting assistant is powered by the Strands Agents SDK with Amazon Bedrock. You can customize it in several ways:

1. **Custom LLM Prompts** - Edit the summary prompt templates in the `lma-llm-template-setup-stack` DynamoDB table. Default prompts are provided, and you can override them with custom prompts that are preserved across stack updates.

2. **Chat Button Configuration** - Customize the suggestion buttons in the meeting assistant chat UI through the DynamoDB-based button configuration. Admin users can edit buttons directly from the LMA UI settings.

3. **MCP Server Integration** - Add external tools and capabilities to the Strands agent by configuring MCP (Model Context Protocol) servers through the LMA UI.

4. **Bedrock Knowledge Base** - Deploy with `STRANDS_BEDROCK_WITH_KB (Create)` to automatically create a knowledge base from your documents, or use `STRANDS_BEDROCK_WITH_KB (Use Existing)` to connect to an existing one.

5. **Bedrock Guardrails** - Configure Bedrock Guardrails to control and filter the meeting assistant's responses.

For more details, see the [Meeting Assist README](./lma-meetingassist-setup-stack/README.md).

## Contributing, and reporting issues

We welcome your contributions to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

## Security

See [Security issue notifications](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](./LICENSE) file.
