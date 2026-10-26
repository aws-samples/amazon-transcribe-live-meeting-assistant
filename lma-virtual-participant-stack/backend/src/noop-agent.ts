/**
 * No-Op Voice Assistant Provider
 * 
 * Used when voice assistant is disabled (provider = 'none')
 */

import { VoiceAssistantProvider } from './voice-assistant-interface.js';
import {
  AssistantReconfigureOutcome,
  AssistantReconfigureRequest,
  formatAssistantToggleLog,
} from './assistant-toggles.js';

export class NoOpAgent implements VoiceAssistantProvider {
  async start(): Promise<void> {
    console.log('Voice assistant disabled (provider=none)');
  }

  async stop(): Promise<void> {
    // No-op
  }

  /**
   * There is no assistant to reconfigure. Still logged, so a toggle issued
   * against a meeting with the voice assistant switched off leaves the same
   * single record as one that landed.
   */
  async reconfigure(request: AssistantReconfigureRequest): Promise<AssistantReconfigureOutcome> {
    const outcome: AssistantReconfigureOutcome = {
      result: 'declined',
      changed: [],
      sessionRebuilt: false,
      // Nominal values: this provider has no modes of its own, and the shape
      // needs something to report.
      meetingModeBefore: 'normal',
      meetingModeAfter: 'normal',
      activationModeBefore: 'wake_phrase',
      activationModeAfter: 'wake_phrase',
      detail: 'the voice assistant is not enabled for this meeting',
    };
    console.log(formatAssistantToggleLog(request, outcome));
    return outcome;
  }

  sendAudioChunk(chunk: Buffer): void {
    // No-op - don't send audio anywhere
  }

  sendUserMessage(text: string): void {
    // No-op
  }

  async preConnect(): Promise<void> {
    // No-op
  }

  async activate(duration?: number, initialContext?: string): Promise<void> {
    // No-op
  }

  deactivate(): void {
    // No-op
  }

  /** There is no assistant to reconfigure. */
  supportsModeChanges(): boolean {
    return false;
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

  mute(_reason?: string): void {
    // No-op
  }

  unmute(_reason?: string): void {
    // No-op
  }

  isMuted(): boolean {
    return false;
  }

  requestStopTalking(_reason?: string): boolean {
    // No-op - there is nothing speaking to silence
    return false;
  }
}
