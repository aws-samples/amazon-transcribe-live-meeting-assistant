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
}

/**
 * DynamoDB configuration item interface
 */
interface DynamoDBConfigItem {
  NovaSonicConfigId: string;
  systemPrompt?: string;
  promptMode?: string;
  modelId?: string;
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
    if (defaultConfig.promptMode === 'base' || defaultConfig.promptMode === 'inject' || defaultConfig.promptMode === 'replace') {
      baseConfig.promptMode = defaultConfig.promptMode;
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
  
  // Apply prompt mode logic
  const finalPrompt = applyPromptMode(
    baseConfig.systemPrompt,
    customConfig.systemPrompt,
    promptMode
  );
  
  // Return merged configuration
  return {
    systemPrompt: finalPrompt,
    promptMode: promptMode,
    modelId: customConfig.modelId || baseConfig.modelId,
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
    return defaultPrompt;
  }
  
  switch (mode) {
    case 'inject':
      // Append custom prompt to default prompt
      return `${defaultPrompt}\n\n${customPrompt}`;
    
    case 'replace':
      // Replace entirely with custom prompt
      return customPrompt;
    
    case 'base':
    default:
      // Use custom prompt as-is (replaces default)
      return customPrompt;
  }
}
