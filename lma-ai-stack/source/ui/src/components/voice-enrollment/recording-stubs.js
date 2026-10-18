/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Browser recording APIs that jsdom does not provide.
 *
 * Without these, `startRecording` throws and the portal never reaches its
 * recording state — which is why the page's earlier tests could only observe
 * the record button and never press it. They live in their own module rather
 * than in the test file because one file may define one class.
 */

/**
 * A MediaRecorder stand-in whose `stop()` fires `ondataavailable` and `onstop`
 * synchronously, so a test can drive a whole clip without timers.
 */
export class FakeMediaRecorder {
  constructor() {
    this.state = 'recording';
    this.mimeType = 'audio/webm';
    this.ondataavailable = null;
    this.onstop = null;
  }

  start() {
    this.state = 'recording';
  }

  stop() {
    this.state = 'inactive';
    if (this.ondataavailable) {
      this.ondataavailable({ data: new Blob(['x'], { type: this.mimeType }) });
    }
    if (this.onstop) {
      this.onstop();
    }
  }
}

/**
 * An AudioContext stand-in that decodes to `seconds()` of silence.
 *
 * The length is read through a callback rather than fixed, because the portal's
 * behaviour turns on how much speech has been recorded: a test needs to choose
 * between "enough" and "not nearly enough". The real decoding is covered by
 * pcm.test.js; here it only has to resolve to something of the right shape.
 */
export const audioContextStub = (seconds) =>
  function AudioContextStub() {
    return {
      decodeAudioData: () => {
        const samples = Math.round(16000 * seconds());
        return Promise.resolve({
          numberOfChannels: 1,
          sampleRate: 16000,
          length: samples,
          duration: seconds(),
          getChannelData: () => new Float32Array(samples),
        });
      },
      close: () => {},
    };
  };
