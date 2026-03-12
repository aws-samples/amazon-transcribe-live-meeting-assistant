/**
 * Voice Assistant Factory
 * 
 * Creates the appropriate voice assistant provider based on configuration
 */

import { VoiceAssistantProvider, ProviderConfig } from './voice-assistant-interface.js';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import { NovaAgent } from './nova-agent.js';
import { NoOpAgent } from './noop-agent.js';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { loadNovaSonicConfig, NovaSonicConfig } from './nova-sonic-config-loader.js';

// Initialize DynamoDB client for config loading
const dynamoDbClient = new DynamoDBClient({
  region: process.env.AWS_REGION || 'us-east-1',
});

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

    case 'amazon_nova_sonic':
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
 * Create voice assistant from environment variables (async to support DynamoDB config loading)
 */
export async function createVoiceAssistantFromEnv(): Promise<VoiceAssistantProvider> {
  const provider = (process.env.VOICE_ASSISTANT_PROVIDER || 'none') as 'none' | 'elevenlabs' | 'amazon_nova_sonic';
  const activationMode = (process.env.VOICE_ASSISTANT_ACTIVATION_MODE || 'wake_phrase') as 'always_active' | 'wake_phrase' | 'strands_tool';
  const activationDuration = parseInt(process.env.VOICE_ASSISTANT_ACTIVATION_DURATION || '30');
  
  console.log(`Voice Assistant Configuration:`);
  console.log(`  Provider: ${provider}`);
  console.log(`  Activation Mode: ${activationMode}`);
  console.log(`  Activation Duration: ${activationDuration}s`);

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

  if (provider === 'amazon_nova_sonic') {
    // Load configuration from DynamoDB if table name is provided
    let config: NovaSonicConfig;
    const tableName = process.env.NOVA_SONIC_CONFIG_TABLE_NAME;
    
    if (tableName) {
      try {
        config = await loadNovaSonicConfig(dynamoDbClient, tableName);
        console.log('✓ Loaded Nova Sonic config from DynamoDB');
      } catch (error) {
        console.error('Failed to load Nova Sonic config from DynamoDB:', error);
        // Fallback to environment variables
        config = {
          systemPrompt: process.env.NOVA_SYSTEM_PROMPT || 'You are Alex, an AI meeting assistant. Be concise and helpful.',
          promptMode: 'base',
          modelId: process.env.NOVA_MODEL_ID || 'amazon.nova-2-sonic-v1:0',
        };
      }
    } else {
      // No table configured, use environment variables
      console.log('No Nova Sonic config table specified, using environment variables');
      config = {
        systemPrompt: process.env.NOVA_SYSTEM_PROMPT || 'You are Alex, an AI meeting assistant. Be concise and helpful.',
        promptMode: 'base',
        modelId: process.env.NOVA_MODEL_ID || 'amazon.nova-2-sonic-v1:0',
      };
    }
    
    return createVoiceAssistant({
      ...baseConfig,
      provider: 'amazon_nova_sonic',
      modelId: config.modelId,
      systemPrompt: config.systemPrompt,
      knowledgeBaseId: process.env.NOVA_KNOWLEDGE_BASE_ID, // Optional - for future enhancement
    });
  }

  // Default to no-op
  return createVoiceAssistant({
    ...baseConfig,
    provider: 'none',
  });
}

// Export async function to create singleton instance
// Note: Callers must await this function
let voiceAssistantInstance: VoiceAssistantProvider | null = null;

export async function getVoiceAssistant(): Promise<VoiceAssistantProvider> {
  if (!voiceAssistantInstance) {
    voiceAssistantInstance = await createVoiceAssistantFromEnv();
  }
  return voiceAssistantInstance;
}

// For backward compatibility, export a promise that resolves to the voice assistant
export const voiceAssistant = createVoiceAssistantFromEnv();
