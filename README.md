# Live Meeting Assistant (LMA) with Amazon Transcribe, Amazon Q Business, and Amazon Bedrock 

_Companion AWS blog post: [Live meeting assist with Amazon language AI services](http://www.amazon.com/live-meeting-assist)_

## Overview

_See [CHANGELOG](./CHANGELOG.md) for latest features and fixes._

Have you ever struggled to take notes during a meeting, while still paying attention to the conversation? Or have you had to quickly fact-check something that's been said, or look up information from trusted sources in order to answer a question that's just been asked in the call? Or maybe your boss just joined the meeting late, and pings you on chat for a quick summary of what's been said so far. 
Or, maybe everyone is talking in a language that's not your first language, and you'd love to have live translation of what people are saying. 
And, after the call is over, you may want to capture a summary - for your records or to email to the participants - with a list of all the action items, owners, and due dates. 

All of this, and more, is now possible with our newest sample solution, Live Meeting Assistant (LMA). 

It captures speaker audio and metadata from your browser-based meeting app (Zoom for now, Chime, Teams coming), and uses Amazon Transcribe for speech to text, Amazon Q business for contextual queries against your company's documents and knowledge sources, and Amazon Bedrock for customizable transcription insights and summaries. 

Are you excited yet? Watch the demo below, and then follow the tutorial to deploy it and try it for yourself!

[DEMO](https://broadcast.amazon.com/videos/1057403)


TO BE COMPLETED

## Clean Up



Congratulations! :tada: You have completed all the steps for setting up your live call analytics sample solution using AWS services.

**To make sure you are not charged for any unwanted services, you can clean up by deleting the stack created in the _Deploy_ section and its resources.**

When you’re finished experimenting with this sample solution, clean up your resources by using the AWS CloudFormation console to delete the LiveCallAnalytics stacks that you deployed. This deletes resources that were created by deploying the solution. The recording S3 buckets, the DynamoDB table and CloudWatch Log groups are retained after the stack is deleted to avoid deleting your data.

[(Back to top)](#overview)

## Contributing

Your contributions are always welcome! Please have a look at the [contribution guidelines](CONTRIBUTING.md) first. :tada:

[(Back to top)](#overview)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

[(Back to top)](#overview)

## License Summary

This sample code is made available under the Apache-2.0 license. See the [LICENSE](LICENSE.txt) file.

[(Back to top)](#overview)
