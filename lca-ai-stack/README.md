# Amazon Transcribe Live Call Analytics with Agent Assist (LCA) Sample Solution
The Amazon Transcribe Live Call Analytics with Agent Assist (LCA) Sample Solution provides the combination of speech to text transcription and insights for agents and supervisors all in real-time. This enables agents to better understand customer needs and drive resolution using the insights the solution provides while they are still interacting with their customer.

This sample solution deploys resources that consume audio from any producer that streams to Amazon Kinesis Video Streams (KVS), uses Amazon Transcribe real-time streaming for transcription, Amazon Comprehend for sentiment analysis, and provides a demo website to demonstrate live call analytics capabilities.

## Building distributable for customization

### Requirements

This project is developed and tested on Amazon Linux 2 using AWS Cloud9. These
are the minimum requirements for building the solution:

- [AWS CLI](https://aws.amazon.com/cli/)
- SAM CLI 1.49 or higher - [Install the SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html)
- [Python 3 installed](https://www.python.org/downloads/)
- Docker - [Install Docker community edition](https://hub.docker.com/search/?type=edition&offering=community)

### Build and S3 staging steps

* Configure the bucket name of your target Amazon S3 distribution bucket
```
export DIST_OUTPUT_BUCKET=my-bucket-name # bucket where customized code will reside
export SOLUTION_NAME=my-solution-name
export VERSION=my-version # version number for the customized code
```
_Note:_ You would have to create an S3 bucket with the prefix 'my-bucket-name-<aws_region>'; aws_region is where you are testing the customized solution. Also, the assets in bucket should be publicly accessible.

* Now build and upload the distributable template and artifacts:
```
cd deployment &&
chmod +x ./build-s3-dist.sh \n
./build-s3-dist.sh $DIST_OUTPUT_BUCKET $SOLUTION_NAME $VERSION \n
```

* Get the link of the solution template uploaded to your Amazon S3 bucket (in the output of the previous command).
* Deploy the solution to your account by launching a new AWS CloudFormation stack using the link of the solution template in Amazon S3.

## Connecting this solution with Amazon Chime SDK Voice Connector
_Pre-requisites_: You need to [enable KVS Streaming](https://docs.aws.amazon.com/chime/latest/ag/start-kinesis-vc.html) on the Amazon Chime SDK Voice Connector used in your environment. When you enable Amazon Chime SDK Voice Connector streaming, make sure to select AWS EventBridge as the streaming trigger. **This solution already hooks up an EventBridge rule to the Lambda KVS Consumer and Streaming Transcriber.**
