/**
 * Voice Assistant Factory
 * 
 * Creates the appropriate voice assistant provider based on configuration
 */

import { VoiceAssistantProvider, ProviderConfig } from './voice-assistant-interface.js';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import { NovaAgent } from './nova-agent.js';
import { NoOpAgent } from './noop-agent.js';

/**
 * Create a voice assistant provider based on configuration
 */
export function createVoiceAssistant(config: ProviderConfig): VoiceAssistantProvider {
  console.log(`Creating voice assistant: provider=${config.provider}, mode=${config.activationMode}`);
  
  switch (config.provider) {
    case 'elevenlabs':
      return new ElevenLabsAgent({
        apiKey: config.apiKey,
        agentId: config.agentId,
        activationMode: config.activationMode,
        activationDuration: config.activationDuration,
        region: config.region,
        strandsLambdaArn: config.strandsLambdaArn,
      });

    case 'aws_nova':
      return new NovaAgent({
        modelId: config.modelId,
        systemPrompt: config.systemPrompt,
        knowledgeBaseId: config.knowledgeBaseId,
        activationMode: config.activationMode,
        region: config.region,
        strandsLambdaArn: config.strandsLambdaArn,
      });

    case 'none':
    default:
      return new NoOpAgent();
  }
}

/**
 * Create voice assistant from environment variables
 */
export function createVoiceAssistantFromEnv(): VoiceAssistantProvider {
  const provider = (process.env.VOICE_ASSISTANT_PROVIDER || 'none') as 'none' | 'elevenlabs' | 'aws_nova';
  const activationMode = (process.env.VOICE_ASSISTANT_ACTIVATION_MODE || 'always_active') as 'always_active' | 'wake_phrase' | 'strands_tool';
  const activationDuration = parseInt(process.env.VOICE_ASSISTANT_ACTIVATION_DURATION || '30');

  // Build provider-specific config
  const baseConfig = {
    provider,
    activationMode,
    activationDuration,
  };

  if (provider === 'elevenlabs') {
    return createVoiceAssistant({
      ...baseConfig,
      provider: 'elevenlabs',
      apiKey: process.env.ELEVENLABS_API_KEY || '',
      agentId: process.env.ELEVENLABS_AGENT_ID,
    });
  }

  if (provider === 'aws_nova') {
    return createVoiceAssistant({
      ...baseConfig,
      provider: 'aws_nova',
      modelId: process.env.NOVA_MODEL_ID || 'amazon.nova-2-sonic-v1:0',
      systemPrompt: process.env.NOVA_SYSTEM_PROMPT || 'You are Alex, an AI meeting assistant. Be concise and helpful.',
      knowledgeBaseId: process.env.NOVA_KNOWLEDGE_BASE_ID, // Optional - for future enhancement
    });
  }

  // Default to no-op
  return createVoiceAssistant({
    ...baseConfig,
    provider: 'none',
  });
}

// Export singleton instance created from environment variables
export const voiceAssistant = createVoiceAssistantFromEnv();
