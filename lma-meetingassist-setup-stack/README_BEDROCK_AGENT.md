# Amazon Bedrock Agent integration in LMA (preview)

Amazon Bedrock Agents can orchestrate and execute multistep tasks across company systems and data sources. They use the reasoning abilities of foundation models to break down user requests into logical sequences, automatically calling necessary APIs to fulfill tasks. Agents can securely also connect to company data sources via Bedrock knowledge bases and augment user requests with relevant information to generate accurate responses. 

Now you can bring Amazon Bedrock Agents into your online meetings using Live Meeting Assistant (LMA) - to get answers to questions, to fact check what's said, based on your company data, and now also, to automate tasks like sending an email, creating a ticket, scheduling an appointment, and much more.

## Use cases for agents in LMA
If you want LMA to be able to do more than answer questions or fact checking, you might want to implement an agent to power your LMA Meeting Assistant. In addition to these things, an agent can be designed to 'use tools' in the form of API calls or Lambda functions that allow it to interact with your own company systems and with the world in general, in many ways. For example, use agents to enable your LMA assistant to book appointments, send emails or instant messages, check the weather, look up balances, and much much more! 

Here's a demo of a very simple agent that can use a knowledge base and send emails. 

https://github.com/user-attachments/assets/0bb2b087-89ed-4114-ab9c-1ec5b376dbfc

## Enhanced Action Groups for Business Applications

LMA now supports additional action groups that allow your Bedrock Agent to interact with popular business applications:

### Salesforce Integration
Create opportunities directly in Salesforce during your meetings. When enabled, your agent can:
- Create new sales opportunities with specified amounts and close dates
- Associate opportunities with existing accounts
- Set opportunity stages and descriptions

Example: "OK Assistant, create a Salesforce opportunity for ABC Corp for $50,000 with a close date of next quarter"

### Jira Integration
Create issues and tasks in Jira during your meetings. When enabled, your agent can:
- Create new issues with specified project keys
- Set issue types, priorities, and assignees
- Add detailed descriptions and labels

Example: "OK Assistant, create a Jira bug in project DEMO with high priority titled 'Fix login screen error'"

### Asana Integration
Create tasks in Asana during your meetings. When enabled, your agent can:
- Create new tasks with specified names and descriptions
- Assign tasks to team members
- Set due dates and add tasks to specific projects

Example: "OK Assistant, create an Asana task for Sarah to update the marketing materials by next Friday"

## Current Limitations
Be aware of some limitations when using agents in LMA:
- Using an agent makes the Meeting Assistant slower than integrating directly with a knowledge base or Q Business application. The extra power to do actions comes at the cost of increased latency. 
    - You can minimize this latency by carefully designing your agent to be as efficient as possible, and to use the fastest LLM models.
- LMA requires your agent to return a completed response for each invocation request - it does not support multi-turn interactions currently. Specifically your agent should not:
   - ask for additional information after being invoked, rather it should simply say what information is missing in its response, and the user can try again with a more complete "OK Assistant/Ask Assistant" request.
   - ask the user to confirm actions before executing them
   - return control to the client application
- LMA does not currently use agent session or multi-session memory. Each Meeting Assistant request is a discrete agent invocation, where the cumulative meeting transcript serves as the context (rather than the agent's memory of prior interactions).

We provide two easy options for you to get started:
1. Bring your own agent
2. Let LMA create a demo agent for you

## Bring your own agent

Once you have created and tested your own Bedrock Agent, integrate it with LMA when you deploy or update an LMA CloudFormation stack. 

1. For **Meeting Assist Service**, choose `BEDROCK_AGENT (Use Existing)`
2. For **Bedrock Agent Id (existing)**, enter the agent *ID* (not name) of an existing Bedrock agent to be used for Meeting Assist bot (e.g. T4XXXZPRZN)
3. For **Bedrock Agent Alias Id (existing)**, provide the agent Alias *ID* (not the Alias name) of an existing Bedrock agent version to be used for Meeting Assist bot, or leave the default as `TSTALIASID` to experiment with the current working version of the agent.

and that's it..  Your LMA stack will be configured to use the QnABot Lambda Hook `BedrockAgent-LambdaHook` to invoke your agent when the Meeting Assistant is invoked. For more information on how the QnABot on AWS solution is used in LMA, see the [LMA Meeting Assist README](./README.md).

## Let LMA create a demo agent for you

1. For **Meeting Assist Service**, choose `BEDROCK_AGENT_WITH_KNOWLEDGE_BASE (Create)`
2. Optionally, for **BedrockKnowledgeBaseId**, provide the knowledge base *Id* of an existing Bedrock knowledge base for the new Bedrock Agent, or leave blank to have a new knowledge base created for you.
3. Optionally, for **BedrockKnowledgeBaseWebCrawlerURLs**, or any of the other knowledge base data source parameters, modify the defaults to determine how your new knowledge base is initially populated.
4. To enable business application integrations, set the following parameters:
   - Set **EnableSalesforceIntegration** to `true` to add Salesforce opportunity creation capabilities
   - Set **EnableJiraIntegration** to `true` to add Jira issue creation capabilities
   - Set **EnableAsanaIntegration** to `true` to add Asana task creation capabilities

and that's it..  Your LMA stack will create a simple Bedrock agent for you, and configure it the QnABot Lambda Hook `BedrockAgent-LambdaHook` to invoke this agent when the Meeting Assistant is invoked.

The agent that is created for you has 
- a Bedrock Knowledge for looking up information
- an action group with a Lambda function that can send you messages via an SNS topic.
- optional action groups for Salesforce, Jira, and Asana integrations (if enabled)

**Verifying email for receiving SNS messages** - When the LMA stack creates the new Bedrock Agent, it also provisions an SNS topic, and automatically subscribes your email address (the AdminEmail you provided). You'll get an email at this address from `AWS Notifications <no-reply@sns.amazonaws.com>` with the subject `AWS Notification - Subscription Confirmation`. Open this email and click `Confirm subscription` so that you can play with the new message sending feature of LMA's meeting assistant agent!  You can add additional emails and/or phone numbers for text messages to the SNS Topic subscription in the Amazon SNS console; the SNS Topic is identified in the LMA Stack outputs as `SNSTopicForAgentMessages`.

**Configuring business application credentials** - If you enabled any of the business application integrations (Salesforce, Jira, Asana), you'll need to update the corresponding AWS Secrets Manager secrets with your actual credentials. The secret names are provided in the stack outputs.

Try it - ask "OK Assistant" for some information about life insurance, and then ask it to send the info to you in an email. See example below.

<img src="../images/meetingassist-agent-query-action.png" alt="meetingassist-agent-query-action" width="800">

Here's the email it sent me after the interaction above:  

<img src="../images/meetingassist-agent-example-email.png" border="1" alt="meetingassist-agent-example-email" width="500">

Also try the "ASK ASSISTANT" button to have the meeting assistant agent respond silently to the most recent questions or instructions from the transcript.

<img src="../images/meetingassist-agent-query-action2.png" alt="meetingassist-agent-query-action2" width="900">

Now try your own examples. Get it to fact check incorrect statements, email a summary of action items, create a Salesforce opportunity based on the meeting discussion, create a Jira issue for a bug mentioned in the meeting, or assign tasks in Asana. Find out what works well, and what doesn't, and then see if you can make it work better!
- Can you make it more accurate? Learn all about Bedrock Agents and how to customize prompts.
- Can you add new action groups, for example, to book appointments, create tickets, retrieve balances, etc.

## Experiment

Use the CloudWatch logs from the `BedrockAgent-LambdaHook` function to get additional insights and troubleshooting context for how these prompts are used, and how LMA is interacting with your agent.

## Contributing

Consider this a starting point for you to build on! LMA is open source - we hope it will get you started quickly, but you will discover gaps, and identify opportunities to improve the power of using agents as meeting assistants. Help us make it better by contributing your fixes and enhancements to the project in GitHub.
