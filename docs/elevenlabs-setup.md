# ElevenLabs Voice Assistant Setup Guide

## Overview

This guide walks you through setting up the ElevenLabs Conversational AI voice assistant with Strands agent integration in LMA. The ElevenLabs voice assistant provides real-time voice interaction during meetings with access to meeting history, document search, and web search through the Strands agent tool.

## Prerequisites

- LMA deployed (version 0.2.26 and above)
- ElevenLabs account with Conversational AI access
- AWS account with Lambda access (for Strands agent)

## Step 1: Create ElevenLabs Agent

### 1.1 Sign Up for ElevenLabs

1. Go to [elevenlabs.io](https://elevenlabs.io)
2. Sign up or log in to your account
3. Navigate to **Agents** in the dashboard

### 1.2 Create a New Agent

1. Click **Create Agent**
2. Configure basic settings:
   - **Agent Name:** `LMA Meeting Assistant` (or your preferred name)
   - **Voice:** Select a voice that suits your needs
   - **Language:** Select your primary language

### 1.3 Configure System Prompt

Add a system prompt to guide the agent's behavior. Here's a simple example:

```
You are Alex, a knowledgeable and attentive AI meeting assistant.

You have a professional yet approachable demeanor, designed to support meeting participants with real-time information and assistance.

You're concise and respectful of meeting time, delivering information efficiently while maintaining a helpful presence.

When asked about past meetings, documents, or current information, use the strands_agent tool which is a sub-agent with access to all LMA relevant tools such as meeting history knowledgebase, documents knowledgebase, current meeting transcript, web search.

Keep responses focused on the meeting context and participant needs. Be transparent when you don't have information available.
```

### 1.4 Get Your Agent ID

1. After creating the agent, go to the agent's settings
2. Copy the **Agent ID** (starts with `agent_`)
3. Save this for later use

### 1.5 Get Your API Key

1. Go to **Profile** → **API Keys**
2. Click **Create API Key**
3. Copy the API key (starts with `sk_`)
4. Save this securely - you won't be able to see it again

## Step 2: Configure Strands Agent Tool

### 2.1 Navigate to Agent Tools

1. In your ElevenLabs agent dashboard
2. Go to the **Tools** section
3. Click **Add Tool**

### 2.2 Add Client Tool

You can either:

**Option A: Use the JSON Configuration (Recommended)**

1. Click **Import from JSON**
2. Paste the following configuration:

```json
{
  "type": "client",
  "name": "strands_agent",
  "description": "Delegate complex queries to the Strands agent, which has access to document search, meeting history, web search, and other specialized tools. Use this for questions about documents, past meetings, current information from the web, or any query requiring specialized knowledge or data access.",
  "disable_interruptions": false,
  "force_pre_tool_speech": "auto",
  "tool_call_sound": null,
  "tool_call_sound_behavior": "auto",
  "tool_error_handling_mode": "auto",
  "execution_mode": "immediate",
  "assignments": [],
  "expects_response": true,
  "response_timeout_secs": 30,
  "parameters": [
    {
      "id": "query",
      "type": "string",
      "value_type": "llm_prompt",
      "description": "The user's question or request to be processed by the Strands agent. This should be the complete query that needs specialized knowledge or data access.",
      "dynamic_variable": "",
      "constant_value": "",
      "enum": null,
      "is_system_provided": false,
      "required": true
    }
  ],
  "dynamic_variables": {
    "dynamic_variable_placeholders": {}
  }
}
```

**Option B: Manual Configuration**

1. **Tool Type:** Select `Client`
2. **Tool Name:** `strands_agent` (case-sensitive!)
3. **Description:**
   ```
   Delegate complex queries to the Strands agent, which has access to document search, meeting history, web search, and other specialized tools. Use this for questions about documents, past meetings, current information from the web, or any query requiring specialized knowledge or data access.
   ```
4. **Expects Response:** ✅ Checked
5. **Response Timeout:** `30` seconds
6. **Execution Mode:** `immediate`

### 2.3 Add Tool Parameter

1. Click **Add Parameter**
2. Configure the parameter:
   - **Parameter ID:** `query` (case-sensitive!)
   - **Type:** `String`
   - **Value Type:** `LLM Prompt`
   - **Required:** ✅ Checked
   - **Description:**
     ```
     The user's question or request to be processed by the Strands agent. This should be the complete query that needs specialized knowledge or data access.
     ```

3. Click **Save Tool**

### 2.4 Publish Agent

**Important:** After adding the tool, you must publish the agent for changes to take effect:

1. Click **Publish** in the agent dashboard
2. Confirm the publication
3. Wait for the agent to be deployed (usually takes a few seconds)

## Step 3: Deploy LMA with ElevenLabs Configuration

### 3.1 Update CloudFormation Parameters

When deploying or updating your LMA stack, set these parameters:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `VoiceAssistantProvider` | `elevenlabs` | Enable ElevenLabs voice assistant |
| `VoiceAssistantActivationMode` | `wake_phrase` or `always_active` | Choose activation mode |
| `ElevenLabsApiKey` | `sk_...` | Your ElevenLabs API key from Step 1.5 |
| `ElevenLabsAgentId` | `agent_...` | Your agent ID from Step 1.4 |

### 3.2 Activation Modes

**Wake Phrase Mode** (Recommended)
- Agent activates when user says "Hey Alex"
- Saves costs by only connecting when needed
- Automatically disconnects after 30 seconds of inactivity
- Set: `VoiceAssistantActivationMode=wake_phrase`

**Always Active Mode**
- Agent is always listening
- Responds immediately without wake phrase
- Higher costs (continuous connection)
- Set: `VoiceAssistantActivationMode=always_active`

### 3.3 Optional Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VoiceAssistantActivationDuration` | `30` | Seconds to stay active after wake phrase |
| `ElevenLabsOutputFormat` | `pcm_16000` | Audio output format |
| `ElevenLabsPlaybackRate` | `16000` | Audio playback rate in Hz |

## Step 4: Verify Installation

### 4.1 Check CloudFormation Outputs

1. Go to AWS CloudFormation console
2. Find your LMA stack
3. Go to the **Outputs** tab
4. Verify these outputs exist:
   - `VoiceAssistantProvider`: Should show `elevenlabs`
   - `StrandsLambdaArn`: Should show the Lambda ARN

### 4.2 Test Voice Assistant

1. Start or join a meeting with the virtual participant
2. Say: **"Hey Alex, are you there?"**
3. The agent should respond with voice
4. Try: **"Hey Alex, do we have any recent meetings about testing?"**
5. The agent should:
   - Acknowledge the request
   - Call the strands_agent tool
   - Return results from the Strands Lambda
   - Speak the response naturally

## Step 5: Monitor and Troubleshoot

### 5.1 Check Logs

View logs in CloudWatch:

1. Go to AWS CloudWatch console
2. Navigate to **Log Groups**
3. Find the log group for your virtual participant Lambda
4. Look for these log messages:
   ```
   ✓ ElevenLabs Conversational AI agent enabled
   ✓ Lambda client initialized for Strands agent tool
   🎤 Wake phrase detected, capturing context...
   🔧 Client tool call received: strands_agent
   Invoking Strands agent Lambda...
   ✓ Tool result sent to ElevenLabs
   ```

### 5.2 Check ElevenLabs Dashboard

1. Go to your ElevenLabs agent dashboard
2. Click on **Analytics** or **Logs**
3. View conversation transcripts
4. Check tool call statistics
5. Verify the `strands_agent` tool is being called

## Troubleshooting

### Issue: Agent Not Responding to Wake Phrase

**Cause:** Wake phrase detection not working

**Solution:**
- Speak clearly: "Hey Alex" (pause) "your question"
- Check microphone is working in the meeting
- Try: "Hey Alex, are you there?" as a simple test
- Verify `VoiceAssistantActivationMode=wake_phrase` is set

### Issue: "Tool Call Timed Out"

**Cause:** Response timeout too short for Lambda invocation

**Solution:**
- Verify `response_timeout_secs` is set to `30` in tool configuration
- Check Lambda execution time in CloudWatch
- Increase timeout if Lambda takes longer than 30 seconds

### Issue: Tool Not Being Called

**Cause:** Agent doesn't know when to use the tool or agent not published

**Solution:**
- **Publish the agent** after adding the tool (Step 2.4)
- Make sure tool name is exactly `strands_agent` (case-sensitive)
- Verify parameter name is exactly `query` (case-sensitive)
- Check tool is enabled and assigned to the agent
- Review tool description to ensure it's clear when to use it

### Issue: "WebSocket Connection Closed"

**Cause:** Invalid API key or agent ID

**Solution:**
- Verify API key is correct (starts with `sk_`)
- Verify agent ID is correct (starts with `agent_`)
- Check API key has not expired
- Regenerate API key if needed

### Issue: No Audio Response

**Cause:** Audio configuration issue

**Solution:**
- Check `ElevenLabsOutputFormat=pcm_16000` is set
- Verify `ElevenLabsPlaybackRate=16000` matches output format
- Check virtual participant audio setup
- Review paplay logs in CloudWatch

### Issue: Lambda Not Invoked

**Cause:** Missing Strands Lambda ARN or permissions

**Solution:**
- Verify `STRANDS_LAMBDA_ARN` environment variable is set
- Check Lambda execution role has invoke permissions
- Verify Lambda is in the same region
- Check CloudWatch logs for permission errors

## Architecture

### How It Works

```
User Speech
    ↓
Wake Phrase Detection ("Hey Alex")
    ↓
ElevenLabs Agent Activation
    ↓
User Query → ElevenLabs Agent
    ↓
Agent Decides to Use strands_agent Tool
    ↓
Client Tool Call → LMA Backend
    ↓
Invoke Strands Lambda
    ↓
Lambda Response → ElevenLabs Agent
    ↓
Agent Processes Result
    ↓
Voice Response to User
```

### Components

- **ElevenLabs Agent:** Handles voice conversation and tool decisions
- **LMA Backend:** Registers client tool handler and invokes Lambda
- **Strands Lambda:** Provides access to meeting history, documents, web search
- **Virtual Participant:** Captures audio and plays responses in meeting

## Available Capabilities

Once configured, the voice assistant can:

### Meeting History
- **Example:** "Do we have any recent meetings about testing?"
- **Example:** "What did we discuss in yesterday's meeting?"
- **Example:** "Find meetings with John from last week"

### Document Search
- **Example:** "Search our documents for project requirements"
- **Example:** "What does our policy say about remote work?"
- **Example:** "Find the latest product roadmap"

### Web Search
- **Example:** "What's the latest news about AI?"
- **Example:** "Look up the weather forecast"
- **Example:** "Search for information about AWS Lambda"

### General Assistance
- **Example:** "What can you help me with?"
- **Example:** "Summarize this meeting so far"
- **Example:** "Who's in this meeting?"

## Security Considerations

### API Key Security

- API keys are stored as encrypted CloudFormation parameters
- Never commit API keys to source control
- Rotate API keys periodically
- Use AWS Secrets Manager for production deployments

### Token Management

- ElevenLabs manages session tokens automatically
- WebSocket connections are encrypted (WSS)
- Connections close automatically after inactivity
- No persistent storage of conversation data

### Permissions

- Lambda execution role has minimal required permissions
- Strands Lambda access controlled by IAM
- Virtual participant isolated per meeting
- No cross-meeting data access

## Cost Optimization

### Wake Phrase Mode

- **Recommended for most use cases**
- Only connects when activated
- Automatically disconnects after 30 seconds
- Saves ~90% of connection costs

### Always Active Mode

- Use only when immediate response is critical
- Continuous WebSocket connection
- Higher ElevenLabs API costs
- Consider for high-priority meetings only

### Token Usage

- Each conversation uses ElevenLabs tokens
- Monitor usage in ElevenLabs dashboard
- Set usage limits to control costs
- Consider voice selection (some voices cost more)

## Advanced Configuration

### Custom Wake Phrases

To add custom wake phrases, modify the wake phrase detection in `scribe.ts`:

```typescript
private wakePhrases = ['hey alex', 'ok alex', 'hi alex', 'hello alex', 'hey assistant'];
```

### Custom Activation Duration

Set longer activation for complex queries:

```bash
VoiceAssistantActivationDuration=60  # 60 seconds
```

### Custom Audio Format

For different audio requirements:

```bash
ElevenLabsOutputFormat=pcm_24000  # 24kHz audio
ElevenLabsPlaybackRate=24000      # Match playback rate
```

### Multiple Agents

Deploy different agents for different meeting types:

1. Create multiple ElevenLabs agents
2. Set different agent IDs per deployment
3. Use different system prompts per agent
4. Route meetings to appropriate agents

## Support

For issues specific to:
- **LMA Voice Assistant:** Check LMA documentation and CloudWatch logs
- **ElevenLabs Agent:** Check [ElevenLabs documentation](https://elevenlabs.io/docs)
- **Strands Agent:** Check Strands Lambda logs in CloudWatch
- **Tool Configuration:** Check [ElevenLabs Tools documentation](https://elevenlabs.io/docs/agents-platform/customization/tools/client-tools)

## Summary

✅ **What You Get:**
- Real-time voice assistant in meetings
- Access to meeting history and documents
- Web search capabilities
- Natural conversation with AI
- Automatic tool invocation

✅ **What You Need:**
- ElevenLabs account (free tier available)
- API key and agent ID
- 10 minutes to set up
- AWS Lambda for Strands agent

✅ **Key Features:**
- Wake phrase activation ("Hey Alex")
- Client-side tool calling
- Automatic Lambda invocation
- Secure token management
- Cost-optimized with wake phrase mode

That's it! Your meetings now have an AI voice assistant with access to your organization's knowledge!
