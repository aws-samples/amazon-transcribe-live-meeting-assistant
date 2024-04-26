chrome.runtime.onMessage.addListener(async function (request, sender, sendResponse) {
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
let samplingRate = 44100;
let audioContext;
let displayStream;
let micStream;

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

const convertToMono = (audioSource) => {
  const splitter = audioContext.createChannelSplitter(2);
  const merger = audioContext.createChannelMerger(1);
  audioSource.connect(splitter);
  splitter.connect(merger, 0, 0);
  splitter.connect(merger, 1, 0);
  return merger;
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
    audioProcessor = null;

    displayStream.getTracks().forEach((track) => {
      track.stop();
    });

    micStream.getTracks().forEach((track) => {
      track.stop();
    });

    if (audioContext) {
      audioContext.close().then(() => {
        chrome.runtime.sendMessage({ action: "TranscriptionStopped" });
        console.log('AudioContext closed.');
        audioContext = null;
      });
    }
  }
}

const startStreaming = async (sendResponse) => {
  try {
    audioContext = new window.AudioContext({
      sampleRate: 8000
    });
    /* Get display media works */
    displayStream = await navigator.mediaDevices.getDisplayMedia({
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
      stopStreaming();
    };

    micStream = await navigator.mediaDevices.getUserMedia({
      video: false,
      audio: {
        noiseSuppression: true,
        autoGainControl: true,
        echoCancellation: true,
      }
    });

    samplingRate = audioContext.sampleRate;
    console.log("Sending sampling rate:", samplingRate);
    chrome.runtime.sendMessage({ action: "SamplingRate", samplingRate: samplingRate });

    let displayAudioSource = audioContext.createMediaStreamSource(displayStream);
    let micAudioSource = audioContext.createMediaStreamSource(micStream);

    let monoDisplaySource = convertToMono(displayAudioSource);
    let monoMicSource = convertToMono(micAudioSource);

    let channelMerger = audioContext.createChannelMerger(2);
    monoMicSource.connect(channelMerger, 0, 0);
    monoDisplaySource.connect(channelMerger, 0, 1);

    try {
      await audioContext.audioWorklet.addModule('audio-worklet.js');
    } catch (error) {
      console.log(`Add module error ${error}`);
    }

    audioProcessor = new AudioWorkletNode(audioContext, 'recording-processor');
    audioProcessor.port.onmessageerror = (error) => {
      console.log(`Error receving message from worklet ${error}`);
    };

    audioProcessor.port.onmessage = async (event) => {
      // this is pcm audio
      //sendMessage(event.data);
      let base64AudioData = await bytesToBase64DataUrl(event.data);
      let payload = { action: "AudioData", audio: base64AudioData };
      chrome.runtime.sendMessage(payload);
    };
    channelMerger.connect(audioProcessor);
    

    // buffer[0] - display stream,  buffer[1] - mic stream
    /*audioProcessor.port.onmessage = async (event) => {
      let audioData = new Uint8Array(
        interleave(event.data.buffer[0], event.data.buffer[1]),
      );
      let base64AudioData = await bytesToBase64DataUrl(audioData);
      // send audio to service worker:
      let payload = { action: "AudioData", audio: base64AudioData };
      chrome.runtime.sendMessage(payload);
    };*/
  } catch (error) {
    // console.error("Error in recorder", error);
    await stopStreaming();
  }
};

console.log("Inside the recorder.js");