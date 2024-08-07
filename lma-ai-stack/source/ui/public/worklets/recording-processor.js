// Based on sample from 
// https://github.com/GoogleChromeLabs/web-audio-samples/blob/main/src/audio-worklet/migration/worklet-recorder/recording-processor.js

class RecordingProcessor extends AudioWorkletProcessor {

  floatTo16BitPCM = (input) => {
    const output = new Int16Array(input.length);
    for (let i = 0; i < input.length; i += 1) {
      const s = Math.max(-1, Math.min(1, input[i]));
      output[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return output;
  };

  decodeWebMToAudioBuffer = (audioBuffer) => {
    const left32Bit = audioBuffer[0];
    const right32Bit = audioBuffer[1];
    const left16Bit = this.floatTo16BitPCM(left32Bit);
    const right16Bit = this.floatTo16BitPCM(right32Bit);
    const length = left16Bit.length + right16Bit.length;
    const interleaved = new Int16Array(length);

    for (let i = 0, j = 0; i < length; j += 1) {
      interleaved[(i += 1)] = left16Bit[j];
      interleaved[(i += 1)] = right16Bit[j];
    }

    return interleaved;
  };


  process(inputs, outputs, parameters) {
    const input = inputs[0];

    if (input && input.length > 0) {
      const outputData = this.decodeWebMToAudioBuffer(input);
      this.port.postMessage(outputData);
    }

    return true;
  }
}

registerProcessor('recording-processor', RecordingProcessor);
