chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "StartTranscription") {
    console.log("Received recorder start streaming message", request);
    startStreaming();
  } else if (request.action === "StopTranscription") {
    console.log("Received recorder stop streaming message", request);
    stopStreaming();
  }
});

/* globals */
let audioProcessor = undefined;

/* Helper funcs */
const bytesToBase64DataUrl = async (bytes, type = "application/octet-stream") => {
  return await new Promise((resolve, reject) => {
    const reader = Object.assign(new FileReader(), {
      onload: () => resolve(reader.result),
      onerror: () => reject(reader.error),
    });
    reader.readAsDataURL(new File([bytes], "", { type }));
  });
}

const pcmEncode = (input) => {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < input.length; i += 1) {
    const s = Math.max(-1, Math.min(1, input[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
};

const interleave = (lbuffer, rbuffer) => {
  const leftAudioBuffer = pcmEncode(lbuffer);
  const leftView = new DataView(leftAudioBuffer);

  const rightAudioBuffer = pcmEncode(rbuffer);
  const rightView = new DataView(rightAudioBuffer);

  const buffer = new ArrayBuffer(leftAudioBuffer.byteLength * 2);
  const view = new DataView(buffer);

  for (let i = 0, j = 0; i < leftAudioBuffer.byteLength; i += 2, j += 4) {
    view.setInt16(j, leftView.getInt16(i, true), true);
    view.setInt16(j + 2, rightView.getInt16(i, true), true);
  }
  return buffer;
};

const stopStreaming = async () => {
  console.log("recorder stop streaming");
  if (audioProcessor && audioProcessor.port) {
    audioProcessor.port.postMessage({
      message: 'UPDATE_RECORDING_STATE',
      setRecording: false,
    });
    audioProcessor.port.close();
    audioProcessor.disconnect();
  }
}

const startStreaming = async () => {
  try {
    let audioContext = new window.AudioContext();
    /* Get display media works */
    let displayStream = await navigator.mediaDevices.getDisplayMedia({
      preferCurrentTab: true,
      video: true,
      audio: {
        noiseSuppression: true,
        autoGainControl: true,
        echoCancellation: true,
      }
    });

    // hook up the stop streaming event
    displayStream.getAudioTracks()[0].onended = () => {
      chrome.runtime.sendMessage({ action: "UserStoppedRecording" });
    };

    let micStream = await navigator.mediaDevices.getUserMedia({
      video: false,
      audio: {
        noiseSuppression: true,
        autoGainControl: true,
        echoCancellation: true,
      }
    });

    let samplingRate = audioContext.sampleRate;
    chrome.runtime.sendMessage({action: "SamplingRate", samplingRate: samplingRate});

    let displayAudioSource = audioContext.createMediaStreamSource(
      new MediaStream([displayStream.getAudioTracks()[0]]),
    );
    let micAudioSource = audioContext.createMediaStreamSource(
      new MediaStream([micStream.getAudioTracks()[0]]),
    );

    channelMerger = audioContext.createChannelMerger(2);
    displayAudioSource.connect(channelMerger, 0, 0);
    micAudioSource.connect(channelMerger, 0, 1);

    try {
      await audioContext.audioWorklet.addModule('audio-worklet.js');
    } catch (error) {
      console.log(`Add module error ${error}`);
    }

    audioProcessor = new AudioWorkletNode(audioContext, 'recording-processor', {
      processorOptions: {
        numberOfChannels: 2,
        sampleRate: samplingRate,
        maxFrameCount: (audioContext.sampleRate * 1) / 10,
      },
    });

    audioProcessor.port.postMessage({
      message: 'UPDATE_RECORDING_STATE',
      setRecording: true,
    });

    let destination = audioContext.createMediaStreamDestination();
    channelMerger.connect(audioProcessor).connect(destination);

    audioProcessor.port.onmessageerror = (error) => {
      console.log(`Error receving message from worklet ${error}`);
    };

    // buffer[0] - display stream,  buffer[1] - mic stream
    audioProcessor.port.onmessage = async (event) => {
      let audioData = new Uint8Array(
        interleave(event.data.buffer[0], event.data.buffer[1]),
      );
      let base64AudioData = await bytesToBase64DataUrl(audioData);
      // send audio to service worker:
      let payload = { action: "AudioData", audio: base64AudioData };
      chrome.runtime.sendMessage(payload);
    };
  } catch (error) {
    // console.error("Error in recorder", error);
    await stopStreaming();
  }
  
};

console.log("Inside the recorder.js");