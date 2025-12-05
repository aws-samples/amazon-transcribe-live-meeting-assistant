import { Sha256 } from '@aws-crypto/sha256-js';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { HttpRequest } from '@smithy/protocol-http';
import { SignatureV4 } from '@smithy/signature-v4';
import WebSocket from 'ws';

/**
 * AppSync Events API WebSocket Client
 * 
 * Implements the correct Event API WebSocket protocol using Smithy SigV4 signing
 * Based on AWS AppSync Events documentation and verified working implementation
 */
export class AppSyncEventsClient {
  private ws: WebSocket | null = null;
  private readonly eventsUrl: string;
  private readonly region: string;
  private readonly httpDomain: string;
  private subscriptions: Map<string, { channel: string; callback: (data: any) => void }> = new Map();
  private reconnectAttempts = 0;
  private readonly maxReconnectAttempts = 5;
  private reconnectTimeout: NodeJS.Timeout | null = null;
  private connectionTimeoutMs = 300000;

  private readonly AWS_APPSYNC_EVENTS_SUBPROTOCOL = 'aws-appsync-event-ws';
  private readonly DEFAULT_HEADERS = {
    accept: 'application/json, text/javascript',
    'content-encoding': 'amz-1.0',
    'content-type': 'application/json; charset=UTF-8',
  };

  constructor(eventsUrl: string, region: string) {
    this.eventsUrl = eventsUrl;
    this.region = region;
    
    // Extract HTTP domain from WebSocket URL
    // wss://xxx.appsync-realtime-api... → https://xxx.appsync-api...
    const wsDomain = eventsUrl.replace('wss://', '').replace('/event/realtime', '').replace('/event', '');
    this.httpDomain = wsDomain.replace('appsync-realtime-api', 'appsync-api');
  }

  /**
   * Sign request using Smithy SignatureV4
   */
  private async sign(body: string): Promise<Record<string, string>> {
    const credentials = defaultProvider();

    const signer = new SignatureV4({
      credentials,
      service: 'appsync',
      region: this.region,
      sha256: Sha256,
    });

    const url = new URL(`https://${this.httpDomain}/event`);
    const httpRequest = new HttpRequest({
      method: 'POST',
      headers: { ...this.DEFAULT_HEADERS, host: url.hostname },
      body,
      hostname: url.hostname,
      path: url.pathname,
    });

    const signedReq = await signer.sign(httpRequest);
    return { host: signedReq.hostname, ...signedReq.headers };
  }

  /**
   * Create base64Url encoded auth protocol header
   */
  private getAuthProtocol(auth: Record<string, string>): string {
    const based64UrlHeader = Buffer.from(JSON.stringify(auth))
      .toString('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
    return `header-${based64UrlHeader}`;
  }

  /**
   * Connect to AppSync Events API WebSocket
   */
  async connect(): Promise<void> {
    try {
      console.log('[Events API] Connecting to AppSync Events API...');
      
      // Sign connection request
      const authHeaders = await this.sign('{}');
      const authProtocol = this.getAuthProtocol(authHeaders);
      
      // Connect with correct protocol
      const wsUrl = this.eventsUrl.includes('/realtime') 
        ? this.eventsUrl 
        : this.eventsUrl.replace('/event', '/event/realtime');
      
      const protocols = [this.AWS_APPSYNC_EVENTS_SUBPROTOCOL, authProtocol];
      
      console.log(`[Events API] Connecting to: ${wsUrl}`);
      this.ws = new WebSocket(wsUrl, protocols);

      return new Promise((resolve, reject) => {
        if (!this.ws) {
          reject(new Error('WebSocket not initialized'));
          return;
        }

        this.ws.on('open', () => {
          console.log('[Events API] WebSocket connected');
          // Resolve immediately - don't wait for subscribe
          resolve();
        });

        this.ws.on('message', (data: Buffer) => {
          const message = JSON.parse(data.toString());
          
          if (message.type === 'ka') {
            // Keep-alive - no logging needed
            return;
          }
          
          console.log('[Events API] Received message:', message.type);
          
          if (message.type === 'subscribe_success') {
            console.log(`[Events API] Subscribe success: ${message.id}`);
            this.reconnectAttempts = 0;
          } else if (message.type === 'data') {
            // Handle incoming event
            const subscriptionId = message.id;
            const subscription = this.subscriptions.get(subscriptionId);
            if (subscription) {
              console.log('[Events API] Received event on subscription:', subscriptionId);
              // Parse event data (it's a JSON string)
              try {
                const eventData = JSON.parse(message.event);
                subscription.callback(eventData);
              } catch (e) {
                console.error('[Events API] Failed to parse event:', e);
              }
            }
          } else if (message.type === 'error' || message.type === 'connection_error' || message.type === 'subscribe_error') {
            console.error('[Events API] Error:', JSON.stringify(message, null, 2));
            reject(new Error(`Event API error: ${message.type}`));
          }
        });

        this.ws.on('error', (error) => {
          console.error('[Events API] WebSocket error:', error);
          reject(error);
        });

        this.ws.on('close', (code, reason) => {
          console.log(`[Events API] WebSocket closed: ${code} - ${reason}`);
          this.handleDisconnect();
        });
      });
    } catch (error) {
      console.error('[Events API] Failed to connect:', error);
      throw error;
    }
  }

  /**
   * Subscribe to an event channel
   */
  async subscribe(channel: string, callback: (data: any) => void): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error('WebSocket not connected. Call connect() first.');
    }

    // Generate unique subscription ID
    const subscriptionId = `sub-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    
    // Store subscription
    this.subscriptions.set(subscriptionId, { channel, callback });

    try {
      // Sign subscribe request
      const authHeaders = await this.sign(JSON.stringify({ channel }));
      
      // Send subscribe message
      const subscribeMessage = {
        type: 'subscribe',
        id: subscriptionId,
        channel: channel,
        authorization: authHeaders
      };

      this.ws.send(JSON.stringify(subscribeMessage));
      console.log(`[Events API] Subscribed to channel: ${channel} (ID: ${subscriptionId})`);
      
    } catch (error) {
      this.subscriptions.delete(subscriptionId);
      throw error;
    }
  }

  /**
   * Unsubscribe from a channel
   */
  unsubscribe(subscriptionId: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[Events API] Cannot unsubscribe - WebSocket not connected');
      return;
    }

    this.subscriptions.delete(subscriptionId);

    const unsubscribeMessage = {
      type: 'unsubscribe',
      id: subscriptionId,
    };

    this.ws.send(JSON.stringify(unsubscribeMessage));
    console.log(`[Events API] Unsubscribed: ${subscriptionId}`);
  }

  /**
   * Handle disconnection and attempt reconnect
   */
  private handleDisconnect(): void {
    this.ws = null;

    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
      
      console.log(`[Events API] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
      
      this.reconnectTimeout = setTimeout(async () => {
        try {
          await this.connect();
          
          // Re-subscribe to all channels
          const subscriptions = Array.from(this.subscriptions.entries());
          for (const [id, sub] of subscriptions) {
            await this.subscribe(sub.channel, sub.callback);
          }
        } catch (error) {
          console.error('[Events API] Reconnection failed:', error);
        }
      }, delay);
    } else {
      console.error('[Events API] Max reconnection attempts reached');
    }
  }

  /**
   * Disconnect from AppSync Events API
   */
  disconnect(): void {
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    
    this.subscriptions.clear();
    console.log('[Events API] Disconnected');
  }

  /**
   * Check if connected
   */
  isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }
}