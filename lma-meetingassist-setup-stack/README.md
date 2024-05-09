# LMA Meeting Assist

## Table of Contents

1. [Introduction](#introduction)  
1. [Generate Action Items](#generate-action-items) 
1. [Summarize, and Identify Topic](#summarize-and-identify-topic) 
1. [Ask Assistant](#ask-assistant)
1. [Freeform questions](#freeform-questions)
1. [Start Message](#start-message)
1. [Buttons](#buttons)
1. [Create your own items](#create-your-own-items)

## Introduction

Live Meeting Assist (LMA) is a solution which provides users with real-time multi-participant audio transcription, optionally translated into their preferred language, and an integrated AI meeting assistant that uses trusted enterprise data and meeting context to fact-check, look up relevant information, and propose responses. It creates succinct on-demand recaps, insights, and action item lists during and after meetings, securely maintaining an inventory of meeting records. Enterprises can use LMA with an existing Amazon Bedrock Agent/Knowledgebase, or with a bedrock LLM without a knowledge base. LMA integrates with popular meeting platforms and offers improved participant focus, understanding, accuracy, time-saving, and record-keeping efficiency, while supporting enterprise security, compliance, and availability. 

Before continuing, please read the blog post [Live Meeting Assistant (LMA)](https://amazon.com/live-meeting-assistant), deploy LMA, and follow the tutorial to experience the Meeting Assist demo. This is prerequisite context for the rest of this document.


In LMA, the meeting assistant is invoked in one of two ways:
1. A meeting participant says the wake phrase (defined by the **Meeting Assist Wake Phrase Regular Expression** parameter in the LMA Cloudformation stack). The wake phrase defaults to *OK Assistant!*. When using the *OK Assistant!* wake phrase, the meeting assistant response is inserted and persisted inline with the meeting transcript.
    <img src="../images/readme-OK-Assistant.png" alt="OK Assistant" width=400  style="display: block; margin-left: 0; margin-right: auto;" />

2. The LMA user invokes the meeting assistant using the **Meeting Assist Bot** on the LMA User Interface, by typing a question, or by using one of the built-in 'easy button' options. When using the Meeting Assist Bot on the UI, the responses are shown in the bot panel, and are not persisted after the session.
   <img src="../images/meetingassist-bot.png" alt="Bot" width="200" style="display: block; margin-left: 0; margin-right: auto;" /> 

In either case, LMA sends a request to the [QnABot on AWS solution](https://aws.amazon.com/solutions/implementations/aws-qnabot/) which is used to handle intent configuration and orchestration of backend services. QnABot attempts to match the incoming request to a preconfigured item (or record) that is stored in its own Amazon Opensearch vectorstore. If the request contains the explicit ID of an item (QID) then the match is explicit, otherwise the content of the request is used to perform a vector search to find the most semantically similar match. You can dig deeper into this topic by reading the [LLM text embedding section](https://docs.aws.amazon.com/solutions/latest/qnabot-on-aws/semantic-question-matching.html) of the QnABot Implementation Guide.   

If QnABot finds a good match for the request (*for semantic matches, the `EMBEDDINGS_SCORE_THRESHOLD` setting value defines 'good'*), the matched item determines how the request is fulfilled. If QnABot does not find a good match, it automatically uses a fallback item known as the `no_hits` item to determine how the request is fulfilled.

When you deploy LMA, a default set of items is loaded into QnABot, allowing it to handle all the default meeting assistant features illustrated in the blog. But you aren't limited to using the defaults! You can use QnAbot's Content Designer to inspect, and even to customize the set of items. Log in to QnAbot Designer using the password you configured when you followed the instructions to deploy LMA. Or if you didn't set a password then, locate the email you received with the temporary password for QnABot, and do it now. When you log in, you see all the default items listed:
  
  <img src="../images/meetingassist-qnabot-designer-default-items.png" alt="Designer" style="display: block; margin-left: 0; margin-right: auto;" /> 


## Generate Action Items

Choose the **ACTIONS** button to ask the meeting assistant to generate an action item list from the meeting transcript. You can do this for a meeting while it is in progress, or after it has ended.

  <img src="../images/meetingassist-bot-actions.png" alt="Bot" width=200 style="display: block; margin-left: 0; margin-right: auto;"/> 

When you click **ACTIONS** the bot UI sends a request to QnABot (using Amazon Lex) that identified the ID of the item that deals with Action Items, `AA.ActionItems`.  

In QnAbot Designer, select the `AA.ActionItems` item to see its definition. Note that it has a **Lambda Hook** function defined. You can read about [Lambda Hooks in the QnAbot Implementation Guide](https://docs.aws.amazon.com/solutions/latest/qnabot-on-aws/specifying-lambda-hook-functions.html), but for now, just know that this configures QnABot to invoke the specified function every time the user requests ActionItems. Here we use the 'SummarizeCall' function that was deployed with LMA, and we specify the LLM prompt it should use, in the **Argument** field. 

  <img src="../images/meetingassist-bot-actions-qid.png" alt="ActionItemsQID" style="display: block; margin-left: 0; margin-right: auto;"/> 

The default prompt for generating actions items is:
```
<br><br>Human:<br>{transcript}<br><br>Carefully review the above meeting transcript, and create a numbered list of action items, identifying action item description, owner, and due date. Use markdown bold tags to format the list and each field clearly.<br><br>Assistant:
```
The `<br>` tags are replaced in the function by newlines, and the placeholder `{transcript}` is replaced by the actual transcript, so the LLM prompt is actually more like:
```


Human:
Bob Strahan: All right team, let's get started. 
Chris Lott: OK, I'm ready.
Bob Strahan: We've been assigned this new project, and it needs to be built using aws services.
Chris Lott: What does it need to do?
etc..

Carefully review the above meeting transcript, and create a numbered list of action items, identifying action item description, owner, and due date. Use markdown bold tags to format the list and each field clearly.

Assistant:
```

You may be able to improve on this default prompt to get better results. Please, have a go. First make a copy of the original prompt, so you can easily restore it if needed, and then use QnAbot Designer to edit the `AA.ActionItems` item, open the 'advanced' pane, and find and edit the Lambda Hook **Argument**.  Then choose **UPDATE** to save your changes, and try clicking the **ACTION ITEMS** button in the LMA Meeting Assist Bot pane again, and see how your new prompt works.

For extra 'behind the scenes' visibility, go to the QNA-SummarizeCall Lambda function in the AWS Lambda console, inspect the code, and view its logs in CloudWatch. This will help you to understand how it works, and to troubleshoot any issues.

## Summarize, and Identify Topic

The **SUMMARIZE** and **TOPIC** buttons work the same way as **ACTIONS**. They also use the QNA-SummarizeCall Lambda Hook function, but they use different prompts defined in the Lambda Hook Argument field of items `AA.SummarizeCall` and `AA.CurrentTopic` respectively. You can customize these prompts too, as described above.

## Ask Assistant

The **ASK ASSISTANT!** button works similarly, but it uses a different Lambda Hook function than the Summarize, Topic, and Actions items. 

In QnAbot Designer, select the `AA.AskAssistant` item to see its definition. Note that it has a different **Lambda Hook** function. Here we use the 'BedrockKB-LambdaHook' function that was also deployed with LMA - this function interacts with Knowledge bases for Bedrock. If you elected not to integrate a knowledge base by selecting `BEDROCK_LLM` as the Meeting Assist Service when you deployed LMA, then you will see 'BedrockLLM-LambdaHook' function here instead.

The Lambda Hook function retrieves the meeting transcript, and truncates it if needed to represent the last N turns, where N is the value of the QnABot Setting `LLM_CHAT_HISTORY_MAX_MESSAGES` (default is 20, but you can change it in QnABot designer Settings page). The transcript is used to provide context for the prompt.

A JSON object with several parameters is in the **Argument** field. 

  <img src="../images/meetingassist-bot-askassistant-qid.png" alt="AskAssistantQID" style="display: block; margin-left: 0; margin-right: auto;"/> 

The default Argument value string is the following JSON:

```
{"Prompt":"Assistant, what is a good response?", "AnswerPrefix":"Assistant Answer:", "QueryPrefix":"Assistant Query:", "ShowContextText":true, "ShowSourceLinks":true}
```

The fields are:
- Key: "Prompt", Value: "Assistant, what is a good response?"
    - This is the default prompt for 'Ask Assistant', it is interpreted (by an LLM inference) in the context of the meeting transcript. The intention of this prompt is to simply advise the LMA user what might be a good thing to say next, given the topics being discussed and how the meeting is going. 
- Key: "AnswerPrefix", Value: "Assistant Answer:"
    - This is a message to prefix to the response returned by Knowledge base.
- Key: "QueryPrefix", Value: "Assistant Query:"
    - If set, the *Assistant Query* is prefixed to the answer, clearly showing the question that the assistant answers. Remove this key to disable the *Assistant Query* prefix.
- Key: "ShowContextText": Value: true
    - Include relevant passage snippets from your Knowledge Base, used to generate the response. 
- Key: "ShowSourceLinks": Value: true
    - include URLs to the relevant source documents from your Knowledge Base.

As before, you are empowered to tinker with the values to customize or improve on the responses you get from **ASK ASSISTANT!**. For extra 'behind the scenes' visibility, go to the Lambda Hook function in the AWS Lambda console, inspect the code, and view its logs in CloudWatch. This will help you to understand how it works, and to troubleshoot any issues.

## Freeform questions

Instead of choosing one of the 'easy buttons' in the Meeting assist bot UI, you can type a freeform question.

  <img src="../images/meetingassist-bot-freeform.png" alt="Bot freeform" width=200 style="display: block; margin-left: 0; margin-right: auto;"/> 

OR, you can use the wake phrase, **OK Assistant!** to ask the meeting assistant a question using voice during the call. 

  <img src="../images/readme-OK-Assistant.png" alt="OK Assistant" width=400 style="display: block; margin-left: 0; margin-right: auto;"/> 

In both these cases, when QnABot receives your message, it cannot immediately locate a matching ID in its list of items, since your question is free form (not a pre-configured QID). So instead, it will perform a semantic search to see if there is an item with stored questions that is a "good match" - if so, it will use that item to formulate a response - but if there is no good match, it falls back to matching the item with ID `CustomNoMatches` (and the question `no_hits`). 

Select `CustomNoMatches` item in Designer:

  <img src="../images/meetingassist-qnabot-designer-no_hits_qid.png" alt="Bot No_hits" style="display: block; margin-left: 0; margin-right: auto;"/> 

This item, too, has a Lambda Hook function. It also uses either the 'BedrockKB-LambdaHook' or the 'BedrockLLM-LambdaHook' function, but note that here, unlike in the previous 'AA.AskAssistant' item, there is no value for "Prompt" in the Lambda Hook Argument JSON value.  Rather than using a predefined value for Prompt, the prompt is the actual question that you typed or spoke.  Your question is used in the context of the meeting transcription, so it can refer to recent statements and topics being discussed. 

## Start Message

When you first load the LMA Meeting detail page, the Meeting Assist bot displays a welcome message. 

  <img src="../images/meetingassist-bot-start.png" alt="start message"style="display: block; margin-left: 0; margin-right: auto;"/> 


Select `AA.StartMessage` item in Designer and edit the 'Markdown Answer' to change the start message.

  <img src="../images/meetingassist-qnabot-designer-start-item.png" alt="start item" style="display: block; margin-left: 0; margin-right: auto;"/> 

## Buttons

Buttons are defined in each item in Designer, as part of the response generated for that item. To add, delete, or modify buttons, you edit each item where buttons are defined, make your changes, and then choose **UPDATE** to save the changed item. By default, all the built-in items return similar button values, so no matter which button is chosen, the response presents consistent options for the next selection.

  <img src="../images/meetingassist-qnabot-designer-buttons.png" alt="buttons" style="display: block; margin-left: 0; margin-right: auto;"/> 

Use the `QID::` syntax (as shown) if you have an explicit item that you want to match with your button press. Your item can return a static response (like the `AA.StartMessage` item), or it can use a Lambda Hook to return dynamic answers, like the examples above. 

## Create your own items

Create new items in QnABot designer to add capabilities to LMA.  

Give you new item an `Item ID`. Use this ID in any button values (`QID::<Item ID>`) as you've seen in the examples above, when you want a button press to explicitly match the item.

Add one or more **Questions/Utterances** to your item to enable semantic search for free form questions. When an LMA user says `OK Assistant, Why is the sky blue?` or types `Why is the sky blue?` in the bot, QnAbot first tries to find a 'good answer' from the set of items, before invoking the no_hits fallback when it asks Knowledge base or the LLM (see [Freeform questions](#freeform-questions)). By configuring one or more questions that are a "good match" (eg `Why is the sky blue`) then QnAbot will match the question to your item instead of the no_hits item, and your item will determine the response that is given by the meeting assistant.

You can configure a static response by entering plain text in the **Answer** field, or (better) choose **Advanced** and enter rich text in the form of Markdown or HTML in the **Alternate Answers / Markdown Answer** field. Your static answer can optionally use Handlebars to enable conditions and substitutions - see [Handlerbars README](https://github.com/aws-solutions/qnabot-on-aws/blob/main/docs/handlebars/README.md). 

Or you can configure a Lambda Hook for completely dynamic answers based on any logic you choose. Use one of the Lambda Hook functions discussed above (`SummarizeCall`, `BedrockKB-LambdaHook`, or `BedrockLLM-LambdaHook`) with your own Argument values. Or use a new Lambda Hook function that you create yourself, to do whatever you want it to do, for example, query your databases, retrieve information or perform actions using API, integrate with other LLMs - you provide the function, and LMA will run it and return its response to the LMA user. 