import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { createSignedFetcher } from 'aws-sigv4-fetch';
import { AppSyncEventsClient } from './events-api-client.js';
import { createHash } from 'crypto';
import { createLogger } from './logger.js';

const log = createLogger('mcp-command-handler');

/**
 * MCP Command Handler for Virtual Participant
 * 
 * This class manages the MCP client connection to Chrome DevTools and handles
 * commands received via AppSync subscriptions from the Strands agent.
 */
export class MCPCommandHandler {
    private vpId: string;
    private callId: string;
    private mcpClient: Client | null = null;
    private graphqlEndpoint: string;
    private eventsApiUrl: string;
    private awsRegion: string;
    private eventsClient: AppSyncEventsClient | null = null;
    private isRunning: boolean = false;
    private availableTools: any[] = [];
    
    constructor(vpId: string, callId: string) {
        this.vpId = vpId;
        this.callId = callId;
        this.graphqlEndpoint = process.env.GRAPHQL_ENDPOINT || '';
        this.eventsApiUrl = process.env.APPSYNC_EVENTS_URL || '';
        this.awsRegion = process.env.AWS_REGION || 'us-east-1';
        
        if (!this.graphqlEndpoint) {
            log.warn('GRAPHQL_ENDPOINT not configured - MCP command handler disabled');
        }
        
        if (!this.eventsApiUrl) {
            log.warn('APPSYNC_EVENTS_URL not configured - MCP command handler disabled');
        }
    }
    
    /**
     * Start the MCP command handler
     * - Initializes MCP client connection to Chrome DevTools
     * - Publishes available tools to AppSync
     * - Subscribes to MCP commands from Strands agent
     */
    async start(): Promise<void> {
        if (!this.graphqlEndpoint) {
            log.info('GraphQL endpoint not configured - skipping MCP handler');
            return;
        }
        
        log.info('🔌 Starting MCP command handler...');
        
        try {
            // Initialize MCP client
            await this.initializeMCP();
            
            // Subscribe to MCP commands via Event API
            await this.subscribeToCommands();
            
            this.isRunning = true;
            log.info('✓ MCP command handler ready');
            log.info(`  VP ID: ${this.vpId}`);
            log.info(`  Call ID: ${this.callId}`);
            log.info(`  Available tools: ${this.availableTools.length}`);
        } catch (error) {
            log.error('Failed to start MCP command handler:', error);
            throw error;
        }
    }
    
    /**
     * Initialize MCP client connection to Chrome DevTools
     * Uses stdio transport to spawn chrome-devtools-mcp as a child process
     */
    private async initializeMCP(): Promise<void> {
        log.info('Initializing MCP client connection to Chrome DevTools...');
        
        try {
            // Create stdio transport to chrome-devtools-mcp
            const transport = new StdioClientTransport({
                command: 'npx',
                args: [
                    '-y',
                    'chrome-devtools-mcp@latest',
                    '--browserUrl=http://127.0.0.1:9222',
                    '--headless=false'
                ]
            });
            
            // Create MCP client
            this.mcpClient = new Client({
                name: 'lma-vp-appsync',
                version: '1.0.0'
            }, {
                capabilities: {}
            });
            
            // Connect to MCP server
            await this.mcpClient.connect(transport);
            log.info('✓ MCP client connected to Chrome DevTools');
            
            // Get available tools
            const toolsList = await this.mcpClient.listTools();
            this.availableTools = toolsList.tools || [];
            log.info(`✓ Retrieved ${this.availableTools.length} MCP tools`);
            
        } catch (error) {
            log.error('Failed to initialize MCP client:', error);
            throw error;
        }
    }
    
    /**
     * Publish available MCP tools to AppSync
     * This allows the UI and Strands agent to know what tools are available
     */
    private async publishTools(): Promise<void> {
        if (!this.mcpClient || this.availableTools.length === 0) {
            log.info('No tools to publish');
            return;
        }
        
        try {
            const mutation = `
                mutation PublishMCPTools($callId: ID!, $tools: String!) {
                    publishMCPTools(CallId: $callId, tools: $tools)
                }
            `;
            
            const toolsJson = JSON.stringify(this.availableTools);
            
            await this.sendGraphQLRequest(mutation, {
                callId: this.callId,
                tools: toolsJson
            });
            
            log.info(`✓ Published ${this.availableTools.length} MCP tools to AppSync`);
        } catch (error) {
            log.error('Failed to publish MCP tools:', error);
            // Non-critical - continue without publishing
        }
    }
    
    /**
     * Subscribe to MCP commands from AppSync Events API
     * Listens for commands sent by the Strands agent
     */
    private async subscribeToCommands(): Promise<void> {
        log.info(`📡 Subscribing to MCP commands for call ${this.callId}...`);
        log.info(`   Events API URL: ${this.eventsApiUrl}`);
        log.info(`   AWS Region: ${this.awsRegion}`);
        
        if (!this.eventsApiUrl) {
            log.error('❌ Events API URL not configured');
            return;
        }
        
        try {
            // Create Events API client
            this.eventsClient = new AppSyncEventsClient(this.eventsApiUrl, this.awsRegion);
            
            // Connect to Events API
            await this.eventsClient.connect();
            log.info('✓ Connected to AppSync Events API');
            
            // Subscribe to MCP commands channel
            // Use first 16 chars of SHA256 hash (Event API has channel length limit)
            const callIdHash = createHash('sha256').update(this.callId).digest('hex').substring(0, 16);
            const channel = `/mcp-commands/${callIdHash}`;
            
            log.info(`   Original CallId: ${this.callId}`);
            log.info(`   Channel hash: ${callIdHash}`);
            
            this.eventsClient.subscribe(channel, async (event: any) => {
                log.info('📨 ========================================');
                log.info('📨 EVENT RECEIVED FROM EVENTS API!');
                log.info('📨 ========================================');
                log.info('   Event data:', JSON.stringify(event, null, 2));
                
                if (event) {
                    log.info(`📨 Received MCP command: ${event.toolName} (${event.commandId})`);
                    log.info(`   CallId: ${event.CallId}`);
                    log.info(`   Arguments: ${event.arguments}`);
                    await this.handleCommand(event);
                } else {
                    log.warn('⚠️  No event data');
                }
            });
            
            log.info('✓ MCP command subscription active (Events API)');
            log.info(`   Channel: ${channel}`);
            log.info('   Waiting for commands...');
            
        } catch (error) {
            log.error('❌ Failed to set up MCP command subscription:', error);
            throw error;
        }
    }
    
    /**
     * Handle an MCP command received from AppSync
     * Executes the tool via MCP client and publishes result back
     */
    async handleCommand(command: any): Promise<void> {
        const startTime = Date.now();
        
        try {
            const { commandId, toolName, arguments: args } = command;
            const toolArgs = typeof args === 'string' ? JSON.parse(args) : args;
            
            log.info(`🔧 Executing MCP tool: ${toolName}`);
            log.info(`   Command ID: ${commandId}`);
            log.info(`   Arguments: ${JSON.stringify(toolArgs)}`);
            
            if (!this.mcpClient) {
                throw new Error('MCP client not initialized');
            }
            
            // Call MCP tool
            const result = await this.mcpClient.callTool({
                name: toolName,
                arguments: toolArgs
            });
            
            const executionTime = Date.now() - startTime;
            
            // Publish result back via AppSync
            await this.publishResult(commandId, true, result, null, executionTime);
            
            log.info(`✓ MCP tool ${toolName} completed in ${executionTime}ms`);
            
        } catch (error) {
            const executionTime = Date.now() - startTime;
            log.error(`❌ MCP tool execution failed:`, error);
            
            await this.publishResult(
                command.commandId,
                false,
                null,
                (error as Error).message,
                executionTime
            );
        }
    }
    
    /**
     * Publish MCP command result back to AppSync
     * This allows the Strands agent to receive the result
     */
    private async publishResult(
        commandId: string,
        success: boolean,
        result: any,
        error: string | null,
        executionTimeMs: number
    ): Promise<void> {
        try {
            const mutation = `
                mutation PublishMCPResult($input: PublishMCPResultInput!) {
                    publishMCPResult(input: $input) {
                        commandId
                        success
                    }
                }
            `;
            
            await this.sendGraphQLRequest(mutation, {
                input: {
                    commandId,
                    CallId: this.callId,
                    success,
                    result: JSON.stringify(result),
                    error,
                    executionTimeMs
                }
            });
            
            log.info(`✓ Published result for command ${commandId}`);
        } catch (error) {
            log.error('Failed to publish MCP result:', error);
        }
    }
    
    /**
     * Send a GraphQL request to AppSync using SigV4 signing
     */
    private async sendGraphQLRequest(query: string, variables: any): Promise<any> {
        try {
            const signedFetch = createSignedFetcher({
                service: 'appsync',
                region: this.awsRegion,
            });
            
            const payload = JSON.stringify({ query, variables });
            
            const response = await signedFetch(this.graphqlEndpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: payload,
            });
            
            if (!response.ok) {
                throw new Error(`GraphQL request failed: HTTP ${response.status}`);
            }
            
            const responseData = await response.json();
            
            if (responseData.errors) {
                throw new Error(`GraphQL errors: ${JSON.stringify(responseData.errors)}`);
            }
            
            return responseData.data;
        } catch (error) {
            log.error('GraphQL request failed:', error);
            throw error;
        }
    }
    
    /**
     * Stop the MCP command handler
     * Cleans up subscriptions and closes MCP client
     */
    async stop(): Promise<void> {
        this.isRunning = false;
        
        if (this.eventsClient) {
            this.eventsClient.disconnect();
            this.eventsClient = null;
        }
        
        if (this.mcpClient) {
            await this.mcpClient.close();
            this.mcpClient = null;
        }
        
        log.info('✓ MCP command handler stopped');
    }
    
    /**
     * Get the list of available MCP tools
     */
    getAvailableTools(): any[] {
        return this.availableTools;
    }
    
    /**
     * Check if the handler is running
     */
    isActive(): boolean {
        return this.isRunning && this.mcpClient !== null;
    }
}