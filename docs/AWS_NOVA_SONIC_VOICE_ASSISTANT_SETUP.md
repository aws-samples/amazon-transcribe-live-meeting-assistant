# AWS Nova Sonic 2 Voice Assistant Setup Guide

## Overview

This guide walks you through setting up the AWS Nova Sonic 2 voice assistant with Strands agent integration in LMA. AWS Nova Sonic 2 provides real-time voice interaction during meetings with access to meeting history, document search, web search, and custom MCP integrations through the Strands agent tool.

## Prerequisites

- LMA deployed (version 0.2.26 and above)
- AWS account with Bedrock access
- AWS Nova Sonic 2 model access enabled in your region

## Key Advantages of AWS Nova Sonic 2

- **Native AWS Integration:** No external API keys or third-party services required
- **Bidirectional Streaming:** Real-time audio processing with low latency
- **Built-in Tool Use:** Native support for tool calling and function execution
- **Cost Effective:** Pay only for what you use with AWS pricing
- **Secure:** All data stays within your AWS environment
- **Async Tool Processing:** Tools execute in background without blocking conversation
- **Pre-Tool Acknowledgment:** Announces tool execution to set user expectations

## Step 1: Enable AWS Nova Sonic 2 Model Access

### 1.1 Check Model Availability

1. Go to AWS Bedrock console
2. Navigate to **Model access** in the left sidebar
3. Verify **AWS Nova Sonic 2** is available in your region
4. If not available, request access or use a supported region

### 1.2 Enable Model Access

1. Click **Manage model access**
2. Find **AWS Nova Sonic 2** in the list
3. Check the box to enable access
4. Click **Save changes**
5. Wait for access to be granted (usually immediate)

## Step 2: Deploy LMA with AWS Nova Sonic 2 Configuration

### 2.1 Required CloudFormation Parameters

When deploying or updating your LMA stack, set these parameters:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `VoiceAssistantProvider` | `aws_nova` | Enable AWS Nova Sonic 2 voice assistant |
| `VoiceAssistantActivationMode` | `wake_phrase` or `always_active` | Choose activation mode |

That's it! AWS Nova Sonic 2 requires no API keys or external configuration.

### 2.2 Activation Modes

**Wake Phrase Mode** (Recommended)
- Agent activates when user says "Hey Alex"
- Saves costs by only connecting when needed
- Automatically disconnects after configurable timeout (default: 30 seconds)
- Set: `VoiceAssistantActivationMode=wake_phrase`

**Always Active Mode**
- Agent is always listening
- Responds immediately without wake phrase
- Higher costs (continuous connection)
- Set: `VoiceAssistantActivationMode=always_active`

### 2.3 Optional Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VoiceAssistantActivationDuration` | `30` | Seconds to stay active after wake phrase |
| `VoiceAssistantWakePhrases` | `hey alex` | Comma-separated wake phrases |

## Step 3: Verify Installation

### 3.1 Check CloudFormation Outputs

1. Go to AWS CloudFormation console
2. Find your LMA stack
3. Go to the **Outputs** tab
4. Verify these outputs exist:
   - `VoiceAssistantProvider`: Should show `aws_nova`
   - `StrandsLambdaArn`: Should show the Lambda ARN

### 3.2 Test Voice Assistant

1. Start or join a meeting with the virtual participant
2. Say: **"Hey Alex, are you there?"**
3. The agent should respond with voice
4. Try: **"Hey Alex, what were the action items from our last meeting?"**
5. The agent should:
   - Acknowledge: "Let me search for that information. This may take a moment."
   - Call the strands_agent tool in the background
   - Return results from the Strands Lambda
   - Speak the response naturally

## Step 4: Monitor and Troubleshoot

### 4.1 Check Logs

View logs in CloudWatch:

1. Go to AWS CloudWatch console
2. Navigate to **Log Groups**
3. Find the log group for your virtual participant
4. Look for these log messages:
   ```
   ✓ AWS Nova Sonic 2 voice assistant enabled
   ✓ Strands agent tool configured
   🎤 Wake phrase detected: "hey alex"
   🔧 Tool call received: strands_agent
   ⏳ Pre-tool acknowledgment: "Let me search for that information..."
   🔄 Async tool execution started
   ✓ Tool result ready, streaming response
   ```

### 4.2 Check Bedrock Metrics

1. Go to AWS CloudWatch console
2. Navigate to **Metrics** → **Bedrock**
3. View Nova Sonic 2 usage metrics:
   - Invocations
   - Audio duration
   - Tool calls
   - Errors

## Troubleshooting

### Issue: Agent Not Responding to Wake Phrase

**Cause:** Wake phrase detection not working

**Solution:**
- Speak clearly: "Hey Alex" (pause) "your question"
- Check microphone is working in the meeting
- Try: "Hey Alex, are you there?" as a simple test
- Verify `VoiceAssistantActivationMode=wake_phrase` is set
- Check CloudWatch logs for wake phrase detection

### Issue: "Model Access Denied"

**Cause:** AWS Nova Sonic 2 model access not enabled

**Solution:**
- Go to Bedrock console → Model access
- Enable AWS Nova Sonic 2 model
- Wait for access to be granted
- Verify your region supports Nova Sonic 2

### Issue: Tool Execution Timeout

**Cause:** Strands agent taking too long to respond

**Solution:**
- Check Strands Lambda execution time in CloudWatch
- Verify Lambda has sufficient memory and timeout
- Check if knowledge base queries are slow
- Review async tool processing logs

### Issue: No Audio Response

**Cause:** Audio configuration issue

**Solution:**
- Check virtual participant audio setup
- Verify PulseAudio is running
- Review paplay logs in CloudWatch
- Check microphone unmute status

### Issue: Session Closes During Tool Use

**Cause:** Session management issue (fixed in v0.2.27)

**Solution:**
- Upgrade to LMA v0.2.27 or later
- Session now stays open during tool execution
- Async processing prevents blocking

## Architecture

### How It Works

```
User Speech
    ↓
Wake Phrase Detection ("Hey Alex")
    ↓
AWS Nova Sonic 2 Activation
    ↓
User Query → Nova Sonic 2
    ↓
Nova Decides to Use strands_agent Tool
    ↓
Pre-Tool Acknowledgment: "Let me search for that information..."
    ↓
Async Tool Execution (Non-Blocking)
    ↓
Invoke Strands Lambda
    ↓
Lambda Response → Nova Sonic 2
    ↓
Nova Processes Result
    ↓
Voice Response to User
```

### Components

- **AWS Nova Sonic 2:** Handles voice conversation, tool decisions, and bidirectional streaming
- **LMA Backend:** Manages tool execution and Lambda invocation
- **Strands Lambda:** Provides access to meeting history, documents, web search, MCP integrations
- **Virtual Participant:** Captures audio and plays responses in meeting

## Available Capabilities

Once configured, the voice assistant can:

### Meeting History
- **Example:** "What were the action items from our last meeting?"
- **Example:** "What did we discuss in yesterday's meeting?"
- **Example:** "Find meetings with John from last week"

### Document Search
- **Example:** "Search our documents for project requirements"
- **Example:** "What does our policy say about remote work?"
- **Example:** "Find the latest product roadmap"

### Web Search (via Tavily)
- **Example:** "What's the latest news about AI voice agents?"
- **Example:** "Search for AWS Nova Sonic 2 documentation"
- **Example:** "What are the best practices for MCP integrations?"

### Salesforce Integration (via MCP)
- **Example:** "Look up the Amazon account in Salesforce"
- **Example:** "What's the contract status for Acme Corp?"
- **Example:** "Show me recent opportunities"

### Slack Integration (via MCP)
- **Example:** "Check Slack for recent messages in the team channel"
- **Example:** "What did Bob say about the demo?"
- **Example:** "Any urgent messages in Slack?"

### Custom MCP Integrations
- **Example:** "Check Jira for open tickets"
- **Example:** "Look up GitHub issues"
- **Example:** "Query our internal database"

## Security Considerations

### No External API Keys Required

- All processing happens within AWS
- No third-party API keys to manage
- No data leaves your AWS environment
- IAM-based access control

### Token Management

- AWS handles authentication automatically
- Bedrock sessions are encrypted
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
- Automatically disconnects after timeout
- Saves ~90% of connection costs

### Always Active Mode

- Use only when immediate response is critical
- Continuous Bedrock connection
- Higher costs
- Consider for high-priority meetings only

### Async Tool Processing

- Tools execute in background (v0.2.27+)
- Nova stays responsive during tool execution
- Better user experience
- No additional cost

### Token Usage

- Pay only for audio duration
- Monitor usage in CloudWatch metrics
- Set budget alerts in AWS Budgets
- Consider wake phrase mode for cost savings

## Advanced Configuration

### Custom Wake Phrases

Set multiple wake phrases:

```bash
VoiceAssistantWakePhrases="hey alex,ok alex,hi alex,hello alex"
```

### Custom Activation Duration

Set longer activation for complex queries:

```bash
VoiceAssistantActivationDuration=60  # 60 seconds
```

## What's New in v0.2.27

### Enhanced Session Management
- Session stays open during tool use and audio playback
- No more premature disconnections
- Smoother conversation flow

### Async Tool Processing
- Tools execute in background without blocking
- Nova remains responsive during tool execution
- Better user experience for complex queries

### Pre-Tool Acknowledgment
- Nova announces: "Let me search for that information. This may take a moment."
- Sets proper user expectations
- Confirmation-based prompting strategy

## Comparison: AWS Nova Sonic 2 vs ElevenLabs

| Feature | AWS Nova Sonic 2 | ElevenLabs |
|---------|------------------|------------|
| **Setup Complexity** | Simple (2 parameters) | Moderate (API key, agent config) |
| **External Dependencies** | None | ElevenLabs account required |
| **API Keys** | Not required | Required |
| **Data Location** | Stays in AWS | Sent to ElevenLabs |
| **Tool Use** | Native Bedrock tool use | Client-side tool calling |
| **Async Processing** | Yes (v0.2.27+) | Depends on configuration |
| **Cost Model** | AWS Bedrock pricing | ElevenLabs pricing |
| **Voice Quality** | High quality | Very high quality |
| **Customization** | Growing (prompts, voices coming) | Extensive |

## Support

For issues specific to:
- **LMA Voice Assistant:** Check LMA documentation and CloudWatch logs
- **AWS Nova Sonic 2:** Check [AWS Bedrock documentation](https://docs.aws.amazon.com/bedrock/)
- **Strands Agent:** Check Strands Lambda logs in CloudWatch
- **Tool Configuration:** Check LMA GitHub repository

## Summary

✅ **What You Get:**
- Real-time voice assistant in meetings
- Access to meeting history and documents
- Web search and MCP integrations
- Natural conversation with AI
- Automatic tool invocation
- Async tool processing (v0.2.27+)

✅ **What You Need:**
- AWS account with Bedrock access
- AWS Nova Sonic 2 model enabled
- 2 CloudFormation parameters
- No external API keys

✅ **Key Features:**
- Wake phrase activation ("Hey Alex")
- Native AWS Bedrock tool use
- Automatic Lambda invocation
- Secure (all in AWS)
- Cost-optimized with wake phrase mode
- Pre-tool acknowledgment
- Async tool execution

That's it! Your meetings now have an AI voice assistant powered by AWS Nova Sonic 2 with access to your organization's knowledge and systems!
