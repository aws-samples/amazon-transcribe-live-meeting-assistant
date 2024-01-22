# Transcript Summarization (Experimental Feature)

LCA can now generate and display an abstractive call transcript summary (rendered in markdown) in addition to the existing extractive summarization from Real Time Transcribe Call Analytics. 

Currently the transcript summarization feature is 'experimental'. In later releases we may adopt different techniques and add capabilities based on feedback from early adoption. We encourage experimentation, and feedback!
  
Example Transcript Summary:
   
![TranscriptSummary](./images/bedrock-summary.png)
   
Transcript Summaries are generated after the call has ended, and can take 20-30 seconds to appear on the UI.

Configure Transcript Summarization by choosing a value for the `EndOfCallTranscriptSummary` CloudFormation parameter when deploying or updating your LCA stack. Valid values are 
`DISABLED`, `BEDROCK`, `SAGEMAKER`, `ANTHROPIC`, and `LAMBDA`.
If `BEDROCK` option is chosen, select a supported model ID from the list (`BedrockModelId` parameter)

### **DISABLED**

This option (default) disables call transcript summarization.

### **BEDROCK** (default)

The `BEDROCK` option is enabled by default. You must [request model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) for the model selected in the `BedrockModelId` parameter. By default, the selected model is `anthropic.claude-instant-v1`.  

When `BEDROCK` option is enabled, LCA can run one or more LLM inferences against Amazon Bedrock after the call is complete. The prompt used to generate the insights is configured in a [AWS Systems Manager Parameter Store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html). The name of the parameter is `{LCA-Stack-Name}-LLMPromptSummaryTemplate`. You can find a link to the parameter in the LCA main stack's CloudFormation outputs.

The parameter's value is a JSON object with key/value pairs, each pair representing the label (key) and the prompt (value). After the call ends, LCA will iterate through the keys and run each prompt. In the prompt, LCA replaces `<br>` tags with newlines, and  `{transcript}` is replaced with the call transcript. The key will be used as a header for the section in the "Transcript Summary" section in the LCA UI.  You can learn more about how each of the prompts are designed in Anthropic's [Introduction to Prompt Design](https://docs.anthropic.com/claude/docs/introduction-to-prompt-design).

Below is the default value of `[LCA-Stack-Name]-LLMPromptSummaryTemplate`: 

```
{
    "Summary":"<br><br>Human: Answer the questions below, defined in <question></question> based on the transcript defined in <transcript></transcript>. If you cannot answer the question, reply with 'n/a'. Use gender neutral pronouns. When you reply, only respond with the answer.<br><br><question>What is a summary of the transcript?</question><br><br><transcript><br>{transcript}<br></transcript><br><br>Assistant:",
    "Topic":"<br><br>Human: Answer the questions below, defined in <question></question> based on the transcript defined in <transcript></transcript>. If you cannot answer the question, reply with 'n/a'. Use gender neutral pronouns. When you reply, only respond with the answer.<br><br><question>What is the topic of the call? For example, iphone issue, billing issue, cancellation. Only reply with the topic, nothing more.</question><br><br><transcript><br>{transcript}<br></transcript><br><br>Assistant:",
    "Follow-Up Actions":"<br><br>Human: Answer the question below, defined in <question></question> based on the transcript defined in <transcript></transcript>. If you cannot answer the question, reply with 'n/a'. Use gender neutral pronouns. When you reply, only respond with the answer.<br><br><question>What follow-up actions did the agent say they are going to take? </question><br><br><transcript><br>{transcript}<br></transcript><br><br>Assistant:"
}
```

The expected output after the summarize step is a single json object, as a string, that contains all the key/value pairs. For example:

```
{
  "Summary": "...",
  "Topic": "...",
  "Follow-Up Actions": "...",
}
```


### **SAGEMAKER**

Choose the `SAGEMAKER` option to deploy a Lambda function and a SageMaker endpoint with the [bart-large-cnn-samsum](https://huggingface.co/philschmid/bart-large-cnn-samsum) model. 

By default a 1-node ml.m5.xlarge endpoint is provisioned. For high call volume deployments, add additional nodes by increasing the parameter `SummarizationSageMakerInitialInstanceCount`. Please check [SageMaker pricing documentation](https://aws.amazon.com/sagemaker/pricing/) for relevant costs and information on Free Tier eligibility. (We have not yet tested summarization with high call volumes, so we don't yet have any scaling guidance. Please experiment and share your feedback.) 
  
By setting the parameter `SummarizationSageMakerInitialInstanceCount` to `0`, a [Serverless SageMaker endpoint](https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints.html) is enabled. A serverless endpoint can save you money by scaling down to zero when not in use, however, there is a 'cold start' time of approximately 1-2 minutes which can delay the availability of the summary when used after a period of inactivity. LCA creates the serverless endpoint with default 4GB model memory, and max concurrency of 50 requests.  

The `CallEventProcessor` Lambda invokes a `SummaryLambda` function at the end of a call. The `SummaryLambda` in turn invokes the `FetchTranscript` Lambda (see more details below) to fetch the full transcript of the call from the DynamoDB table. The transcript is then passed to the SageMaker endpoint to generate a summary.  The summary is returned from the `SummaryLambda` to the `CallEventProcessor`, mutated and persisted to AppSync/DynamoDB, and displayed in the Call Detail page for the call in the LCA UI, as shown above.

NOTES: 
- The summarization model used in this release limits the input text length to 1024 'tokens'. Tokens are words, punctuation, and new lines. Transcripts that are longer that 1024 tokens are automatically truncated to 1024 tokens by the `FetchTranscript` lambda to avoid errors when summarizing. This, unfortunately, reduces the accuracy of the summary for long calls. We hope to be able to increase this limit by adopting newer models in future releases.
- The summarization model used in this release was trained and tested in **English** only. We hope to support additional languages in future releases.

### **ANTHROPIC**

Configure LCA to use 3rd party LLM services from Anthropic by selecting 'ANTHROPIC', and providing an API key issued by the third party provider. Note that when using third party providers, **your data will leave your AWS account and the AWS network** and will be sent in the payload of the API requests to the third party provider. 

When using Anthropic, the latest Claude-2 model is used by default. 

The LCA deployment creates an AWS Lambda function, `LLMAnthropicSummaryLambda`, that interacts with the Anthropic API on your behalf. You can optionally customize the behavior by modifying function enviornment variables:
- ANTHROPIC_API_KEY: Set to the value you provided as `End of Call Summarization LLM Third Party API Key` during deployment.
- ANTHROPIC_MODEL_IDENTIFIER: Set to `claude-2` by default.
- SUMMARY_PROMPT_TEMPLATE: Default prompt template used to generate summary from Claude. Modify this template to customize your summaries. Note, the placeholder `{transcript}` must be present in the prompt template, and will be replaced by the actual call transcript retrieved by `FetchTranscript` Lambda (see more details below). 
- TOKEN_COUNT: Defaults to '0' which means that the transcript is not truncated prior to inference. The default `claude-2` model supports 100k tokens, which we expect to be more than sufficient to handle even the longest call transcripts.


### **LAMBDA**

Use the LAMBDA option to provide your own summarization functions and/or machine learning models. This option allows you to experiment with different models and techniques to customize the summary as you need.

When you choose the `LAMBDA` option, you must provide the Arn of your custom Lambda function in the `EndOfCallLambdaHookFunctionArn` CloudFormation parameter. At the end of a call, the `CallEventProcessor` Lambda function will invoke the custom Lambda and pass in the CallId of the call.

Your custom Lambda function must return the summary in the following JSON format:

```
{
  "summary": "Summary of the call here."
}
```

The summary can optionally use Markdown syntax to include rich text, hyperlinks, images, media, etc.

If you would like to include more than one section in the summary, similar to the `BEDROCK` option, you may provide a JSON-encoded string that contains key value pairs as the summary's value. This will render each key as a section header, and the value as the body of the section. The following is an example:
```
{
  "summary": "{\n\"Summary\":\"This is a summary.\",\n\"Topic\":\"Credit Cards\",\n\"Follow-Up Actions\": \"The agent will send a replacement card.\"\n}"
}
```
This would be rendered as:

![Summary Rendering](images/multipart-summary.png)

Here is a minimal example of a valid custom summarization Lambda function, written in Python. 
```
import json

def lambda_handler(event, context):
    print(json.dumps(event))
    summary = "Placeholder for actual summary" 
    # call your own model, APIs, or summarization functions here...
    return {
      "summary": summary
    }
``` 

This example function trivially returns a hardcoded string as the summary. Your function will be much smarter, and will implement your custom rules or models.
  
Use the provided [FetchTranscript utility Lambda function ](./FetchTranscriptLambda.md) in your custom summarization Lambda to retrieve the call transcript, optionally truncated to the maximum input token limit imposed by your summarization model.

Use the provided SageMaker based `SummaryLambda` function as a reference for creating your own. The function is defined as resource `SummaryLambda` in the CloudFormation template [sagemaker-summary-stack.yaml](./ml-stacks/sagemaker-summary-stack.yaml).

If your custom Lambda fails at runtime, or you do not want to return a summary for the call, return an empty string for the value of the summary field.

## FetchTranscript Utility Lambda

See [FetchTranscriptLambda.md](./FetchTranscriptLambda.md)