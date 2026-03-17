// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Nova Sonic Configuration Loader
 * 
 * This module loads and merges Nova Sonic voice assistant configuration from DynamoDB.
 * It supports three prompt modes:
 * - base: Use custom prompt as-is (or default if no custom)
 * - inject: Append custom prompt to default prompt
 * - replace: Fully replace default with custom prompt
 */

import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, GetCommand } from '@aws-sdk/lib-dynamodb';

/**
 * Nova Sonic configuration interface
 */
export interface NovaSonicConfig {
  systemPrompt: string;
  promptMode: 'base' | 'inject' | 'replace';
  modelId: string;
  voiceId?: string;
  endpointingSensitivity?: 'HIGH' | 'MEDIUM' | 'LOW';
  groupMeetingMode?: boolean;
}

/**
 * DynamoDB configuration item interface
 */
interface DynamoDBConfigItem {
  NovaSonicConfigId: string;
  systemPrompt?: string;
  promptMode?: string;
  modelId?: string;
  voiceId?: string;
  endpointingSensitivity?: string;
  groupMeetingMode?: boolean;
  description?: string;
  '*Information*'?: string;
}

/**
 * Default configuration values (fallback)
 */
const DEFAULT_CONFIG: NovaSonicConfig = {
  systemPrompt: 'You are Alex, an AI meeting assistant. Be concise and helpful.',
  promptMode: 'base',
  modelId: 'amazon.nova-2-sonic-v1:0',
  voiceId: 'tiffany', // Default polyglot voice (English US, feminine)
  endpointingSensitivity: 'MEDIUM', // Default turn-taking sensitivity
  groupMeetingMode: false, // Default to normal mode
};

/**
 * Load Nova Sonic configuration from DynamoDB
 * 
 * @param dynamoDbClient - DynamoDB client instance
 * @param tableName - Name of the configuration table
 * @returns Merged configuration with prompt mode applied
 */
export async function loadNovaSonicConfig(
  dynamoDbClient: DynamoDBClient,
  tableName: string
): Promise<NovaSonicConfig> {
  if (!tableName) {
    console.warn('Nova Sonic config table name not provided, using default configuration');
    return DEFAULT_CONFIG;
  }

  const docClient = DynamoDBDocumentClient.from(dynamoDbClient);

  try {
    // Load default configuration
    const defaultConfig = await getConfigItem(docClient, tableName, 'DefaultNovaSonicConfig');
    
    // Load custom configuration
    const customConfig = await getConfigItem(docClient, tableName, 'CustomNovaSonicConfig');
    
    // Merge configurations
    const mergedConfig = mergeConfigs(defaultConfig, customConfig);
    
    console.log('✓ Loaded Nova Sonic config from DynamoDB:', {
      promptMode: mergedConfig.promptMode,
      modelId: mergedConfig.modelId,
      promptLength: mergedConfig.systemPrompt.length,
    });
    
    return mergedConfig;
  } catch (error) {
    console.error('Failed to load Nova Sonic config from DynamoDB:', error);
    console.warn('Falling back to default configuration');
    return DEFAULT_CONFIG;
  }
}

/**
 * Get a configuration item from DynamoDB
 * 
 * @param docClient - DynamoDB document client
 * @param tableName - Table name
 * @param configId - Configuration ID (DefaultNovaSonicConfig or CustomNovaSonicConfig)
 * @returns Configuration item or null if not found
 */
async function getConfigItem(
  docClient: DynamoDBDocumentClient,
  tableName: string,
  configId: string
): Promise<DynamoDBConfigItem | null> {
  try {
    const command = new GetCommand({
      TableName: tableName,
      Key: {
        NovaSonicConfigId: configId,
      },
    });
    
    const response = await docClient.send(command);
    
    if (!response.Item) {
      console.warn(`Configuration item not found: ${configId}`);
      return null;
    }
    
    return response.Item as DynamoDBConfigItem;
  } catch (error) {
    console.error(`Error fetching config item ${configId}:`, error);
    return null;
  }
}

/**
 * Merge default and custom configurations
 * 
 * @param defaultConfig - Default configuration from DynamoDB
 * @param customConfig - Custom configuration from DynamoDB (may be null)
 * @returns Merged configuration with prompt mode applied
 */
function mergeConfigs(
  defaultConfig: DynamoDBConfigItem | null,
  customConfig: DynamoDBConfigItem | null
): NovaSonicConfig {
  // Start with hardcoded defaults as fallback
  const baseConfig = { ...DEFAULT_CONFIG };
  
  // Apply default config from DynamoDB if available
  if (defaultConfig) {
    if (defaultConfig.systemPrompt) {
      baseConfig.systemPrompt = defaultConfig.systemPrompt;
    }
    if (defaultConfig.modelId) {
      baseConfig.modelId = defaultConfig.modelId;
    }
    if (defaultConfig.voiceId) {
      baseConfig.voiceId = defaultConfig.voiceId;
    }
    if (defaultConfig.promptMode === 'base' || defaultConfig.promptMode === 'inject' || defaultConfig.promptMode === 'replace') {
      baseConfig.promptMode = defaultConfig.promptMode;
    }
    if (defaultConfig.endpointingSensitivity === 'HIGH' || defaultConfig.endpointingSensitivity === 'MEDIUM' || defaultConfig.endpointingSensitivity === 'LOW') {
      baseConfig.endpointingSensitivity = defaultConfig.endpointingSensitivity;
    }
    if (typeof defaultConfig.groupMeetingMode === 'boolean') {
      baseConfig.groupMeetingMode = defaultConfig.groupMeetingMode;
    }
  }
  
  // If no custom config, return base config
  if (!customConfig) {
    return baseConfig;
  }
  
  // Determine prompt mode (custom overrides default)
  const promptMode = (customConfig.promptMode === 'base' ||
                      customConfig.promptMode === 'inject' ||
                      customConfig.promptMode === 'replace')
    ? customConfig.promptMode
    : baseConfig.promptMode;
  
  // Determine endpointing sensitivity (custom overrides default)
  const endpointingSensitivity = (customConfig.endpointingSensitivity === 'HIGH' ||
                                  customConfig.endpointingSensitivity === 'MEDIUM' ||
                                  customConfig.endpointingSensitivity === 'LOW')
    ? customConfig.endpointingSensitivity
    : baseConfig.endpointingSensitivity;
  
  // Apply prompt mode logic
  console.log('🔧 Applying prompt mode logic:');
  console.log(`   Prompt mode: ${promptMode}`);
  console.log(`   Default prompt length: ${baseConfig.systemPrompt.length} chars`);
  console.log(`   Custom prompt length: ${customConfig.systemPrompt?.length || 0} chars`);
  
  const finalPrompt = applyPromptMode(
    baseConfig.systemPrompt,
    customConfig.systemPrompt,
    promptMode
  );
  
  console.log(`   Final merged prompt length: ${finalPrompt.length} chars`);
  
  // Return merged configuration
  return {
    systemPrompt: finalPrompt,
    promptMode: promptMode,
    modelId: customConfig.modelId || baseConfig.modelId,
    voiceId: customConfig.voiceId || baseConfig.voiceId,
    endpointingSensitivity: endpointingSensitivity,
    groupMeetingMode: typeof customConfig.groupMeetingMode === 'boolean'
      ? customConfig.groupMeetingMode
      : baseConfig.groupMeetingMode,
  };
}

/**
 * Apply prompt mode logic to combine default and custom prompts
 * 
 * @param defaultPrompt - Default system prompt
 * @param customPrompt - Custom system prompt (may be undefined)
 * @param mode - Prompt mode (base, inject, or replace)
 * @returns Final system prompt
 */
function applyPromptMode(
  defaultPrompt: string,
  customPrompt: string | undefined,
  mode: 'base' | 'inject' | 'replace'
): string {
  // If no custom prompt, always use default
  if (!customPrompt || customPrompt.trim() === '') {
    console.log('   ⚠️  No custom prompt provided, using default only');
    return defaultPrompt;
  }
  
  console.log('   Custom prompt content:');
  console.log('   ┌' + '─'.repeat(78));
  console.log('   │ ' + customPrompt.substring(0, 200) + (customPrompt.length > 200 ? '...' : ''));
  console.log('   └' + '─'.repeat(78));
  
  let result: string;
  switch (mode) {
    case 'inject':
      // Append custom prompt to default prompt
      console.log('   ✓ Mode: INJECT - Appending custom prompt to default');
      result = `${defaultPrompt}\n\n${customPrompt}`;
      break;
    
    case 'replace':
      // Replace entirely with custom prompt
      console.log('   ✓ Mode: REPLACE - Using custom prompt only');
      result = customPrompt;
      break;
    
    case 'base':
    default:
      // Use custom prompt as-is (replaces default)
      console.log('   ✓ Mode: BASE - Using custom prompt only');
      result = customPrompt;
      break;
  }
  
  return result;
}
