# Configure SNS Notifications on Call Categories

Amazon Transcribe Call Analytics supports the creation of custom categories so you can tailor your transcript analysis to your specific business needs.

You can create as many categories as you'd like to cover a range of different scenarios. For each category you create, you must create between 1 and 20 rules. Each rule is based on one of four criteria: interruptions, keywords, non-talk time, or sentiment. For more details on using these criteria with the [CreateCallAnalyticsCategory](https://docs.aws.amazon.com/transcribe/latest/APIReference/API_CreateCallAnalyticsCategory.html) operation, refer to the [Rule criteria](https://docs.aws.amazon.com/transcribe/latest/dg/call-analytics-create-categories.html#call-analytics-create-categories-rules#call-analytics-create-categories-rules) section.

When LCA receives a category match event from Amazon Transcribe Real-time Call Analytics, it publishes a message to a pre-created [Amazon Simple Notification Service (Amazon SNS) topic](https://docs.aws.amazon.com/sns/latest/dg/welcome.html) that is created during deployment. (*Amazon SNS is a managed service that provides message delivery from publishers (e.g. LCA) to subscribers (also known as producers and consumers).*)

## Subscriptions

LCA creates a new [SNS topic](https://docs.aws.amazon.com/sns/latest/dg/sns-create-topic.html) for each stack that is created, and the name of the SNS topic can be found in the CloudFormation outputs. To subscribe to messages that LCA publishes to SNS, you must [subscribe to the SNS topic](https://docs.aws.amazon.com/sns/latest/dg/sns-create-subscribe-endpoint-to-topic.html). 

SNS subscription types include:

- Application-to-application messaging destinations
  - Amazon Kinesis Data Firehose
  - AWS Lambda
  - Amazon SQS
  - Amazon Event Fork Pipelines
  - HTTP/S endpoints
  
- Application-to-person notifications
  - SMS
  - Email
  - Mobile Push Notifications
  - AWS Chatbot
  - PagerDuty

All category matches will be published to the SNS topic.

## Alerts

LCA provides a CloudFormation parameter called `CategoryAlertRegEx` that allows you to define a [regular expression](https://en.wikipedia.org/wiki/Regular_expression) used to distinguish whether a category match is an alert, or not an alert. This can be used to highlight high-priority notifications, for example, if the agent uses profanity or the caller wants to speak to a supervisor.

Alert category matches show as a different color within the LCA user interface, and will also show as a red notification icon with a count within the call-list user interface.

![CallList](../images/call-list-with-categories-and-alerts.png)

The schema that published by LCA to SNS is as follows:

```
{
  "call_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", 
  "category_name": "MatchedCategory", 
  "alert": true/false
}
```

You can configure SNS with [SNS Filters](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html) to only send messages to subscribers if `alert` is true.

