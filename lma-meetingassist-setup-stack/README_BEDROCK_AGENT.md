# Amazon Bedrock Agent integration in LMA (preview)

[The first few sections remain unchanged]

## Use cases for agents in LMA
If you want LMA to be able to do more than answer questions of fact checking, you might want to implement an agent to power your LMA Meeting Assistant. In addition to these things, an agent can be designed to 'use tools' in the form of API calls or Lambda functions that allow it to interact with your own company systems and with the world in general, in many ways. For example, use agents to enable your LMA assistant to book appointments, send emails or instant messages, check the weather, look up balances, and much much more! 

## Current Limitations
Be aware of some limitations when using agents in LMA:
- Using an agent makes the Meeting Assistant slower than integrating directly with a knowledge base or Q Business application. The extra power to do actions comes at the cost of increased latency. 
    - You can minimize this latency by carefully designing your agent to be as efficient as possible, and to use the fastest LLM models.
- LMA requires your agent to return a completed response for each invocation request - it does not support multi-turn interactions currently. Specifically your agent should not:
   - ask for additional information after being invoked, rather it should simply say what information is missing in its response, and the user can try again with a more complete "OK Assistant/Ask Assistant" request.
   - ask the user to confirm actions before executing them
- LMA does not currently use agent session or multi-session memory. Each Meeting Assistant request is a discrete agent invocation, where the cumulative meeting transcript serves as the context (rather than the agent's memory of prior interactions).

[The "We provide two options" section remains unchanged]

## Bring your own agent

[Most of this section remains unchanged]

and that's it... Your LMA stack will be configured to use the QnABot Lambda Hook `BedrockAgent-LambdaHook` to invoke your agent when the Meeting Assistant is invoked. For more information on how the QnABot on AWS solution is used in LMA, see the [LMA Meeting Assist README](./README.md).

[The "Let LMA create a demo agent for you" and "Experiment with the prompts" sections remain unchanged]

## Contributing

Consider this a starting point for you to build on! LMA is open source - we hope it will get you started quickly, but you will discover gaps, and identify opportunities to improve the power of using agents as meeting assistants. Help us make it better by contributing your fixes and enhancements to the project in GitHub.