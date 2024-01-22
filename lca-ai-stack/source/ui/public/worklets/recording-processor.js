// Based on sample from 
// https://github.com/GoogleChromeLabs/web-audio-samples/blob/main/src/audio-worklet/migration/worklet-recorder/recording-processor.js

class RecordingProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.sampleRate = 0;
    this.maxRecordingFrames = 0;
    this.numberOfChannels = 0;
    this._frameSize = 128;

    if (options && options.processorOptions) {
      const {
        numberOfChannels,
        sampleRate,
        maxFrameCount,
      } = options.processorOptions;

      this.sampleRate = sampleRate;
      this.maxRecordingFrames = maxFrameCount;
      this.numberOfChannels = numberOfChannels;
    }

    this._leftRecordingBuffer = new Float32Array(this.maxRecordingFrames);
    this._rightRecordingBuffer = new Float32Array(this.maxRecordingFrames);
    
    this.recordedFrames = 0;
    this.isRecording = false;

    this.framesSinceLastPublish = 0;
    this.publishInterval = this.sampleRate * 5;

    this.port.onmessage = (event) => {
      if (event.data.message === 'UPDATE_RECORDING_STATE') {
        this.isRecording = event.data.setRecording;
      }
    };
  }

  process(inputs, outputs) {
    let currentSample = 0.0;
    for (let input = 0; input < 1; input++) {
      for (let channel = 0; channel < this.numberOfChannels; channel++) {
        for (let sample = 0; sample < inputs[input][channel].length; sample++) {

          currentSample = inputs[input][channel][sample];

          if (this.isRecording) {
            if (channel == 0) {
              this._leftRecordingBuffer[sample+this.recordedFrames] = currentSample;
            } else if (channel == 1) {
              this._rightRecordingBuffer[sample+this.recordedFrames] = currentSample;
            }
          }
          // Pass data directly to output, unchanged.
          outputs[input][channel][sample] = currentSample;
        }

      }
    }

    const shouldPublish = this.framesSinceLastPublish >= this.publishInterval;

    // Validate that recording hasn't reached its limit.
    if (this.isRecording) {
      if (this.recordedFrames + this._frameSize < this.maxRecordingFrames) {
        this.recordedFrames += this._frameSize;

        // Post a recording recording length update on the clock's schedule
        if (shouldPublish) {
          const recordingBuffer = new Array(this.numberOfChannels)
              .fill(new Float32Array(this.maxRecordingFrames));
          recordingBuffer[0] = this._leftRecordingBuffer;
          recordingBuffer[1] = this._rightRecordingBuffer;
          this.port.postMessage({
            message: 'SHARE_RECORDING_BUFFER',
            buffer: recordingBuffer,
            recordingLength: this.recordedFrames
          });
          this.framesSinceLastPublish = 0;
          this.recordedFrames = 0
        } else {
          this.framesSinceLastPublish += this._frameSize;
        }
      } else {
        this.recordedFrames += this._frameSize;

        const recordingBuffer = new Array(this.numberOfChannels)
            .fill(new Float32Array(this.maxRecordingFrames));
        recordingBuffer[0] = this._leftRecordingBuffer;
        recordingBuffer[1] = this._rightRecordingBuffer;

        this.port.postMessage({
          message: 'SHARE_RECORDING_BUFFER',
          buffer: recordingBuffer,
          recordingLength: this.recordedFrames
        });

        this.recordedFrames = 0;
        this.framesSinceLastPublish = 0;
      } 
    } else {
      console.log('stopping worklet processor node')
      this.recordedFrames = 0;
      this.framesSinceLastPublish = 0;
      return false;
    }

    return true;
  }
}

registerProcessor('recording-processor', RecordingProcessor);
