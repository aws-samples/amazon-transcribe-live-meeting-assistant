/**
 * No-Op Voice Assistant Provider
 * 
 * Used when voice assistant is disabled (provider = 'none')
 */

import { VoiceAssistantProvider } from './voice-assistant-interface.js';

export class NoOpAgent implements VoiceAssistantProvider {
  async start(): Promise<void> {
    console.log('Voice assistant disabled (provider=none)');
  }

  async stop(): Promise<void> {
    // No-op
  }

  sendAudioChunk(chunk: Buffer): void {
    // No-op - don't send audio anywhere
  }

  activate(duration?: number): void {
    // No-op
  }

  deactivate(): void {
    // No-op
  }

  isEnabled(): boolean {
    return false;
  }

  isActive(): boolean {
    return false;
  }

  isActivated(): boolean {
    return false;
  }

  isSpeaking(): boolean {
    return false;
  }
}
