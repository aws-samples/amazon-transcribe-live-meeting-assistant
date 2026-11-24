/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
chrome.runtime.onMessage.addListener(async function (request, sender, sendResponse) {
  if (request.action === "StartTranscription") {
    console.log("Received recorder start streaming message", request);
    startStreaming(request.video === true);
  } else if (request.action === "StopTranscription") {
    console.log("Received recorder stop streaming message", request);
    stopStreaming();
  } else if (request.action === "StartVideo") {
    startVideo();
  } else if (request.action === "StopVideo") {
    stopVideo("stopped");
  }
});

/*
 * Screen video (the side panel's "Also record screen video"): the meeting tab,
 * recorded with MediaRecorder as fragmented MP4 and handed to the side panel
 * chunk by chunk ("VideoChunk"), which sends it to the transcriber on its own
 * connection (src/lib/screen-video.js). Recorded only when the shared surface
 * is the meeting tab itself; anything else leaves the audio running and tells
 * the panel ("VideoUnavailable"). These values match src/lib/screen-video.js,
 * which a test holds them to.
 */
const VIDEO_MIME_TYPES = ['video/mp4;codecs=avc1.640028', 'video/mp4;codecs=avc1'];
const VIDEO_RECORDER_OPTIONS = { videoKeyFrameIntervalDuration: 1800, videoBitsPerSecond: 1500000 };
const VIDEO_TIMESLICE_MS = 2000;
const VIDEO_CONSTRAINTS = { frameRate: { ideal: 5, max: 5 }, width: { max: 1920 }, height: { max: 1920 } };

let videoRecorder = null;
let videoTrack = null;
let videoStopReason = null;

const pickVideoMimeType = () => {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') return null;
  return VIDEO_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || null;
};

const startVideo = () => {
  if (videoRecorder) return;
  if (!videoTrack) {
    // Told now, when the panel is ready for it: the share was not the meeting tab.
    chrome.runtime.sendMessage({ action: "VideoUnavailable", reason: "surface" });
    return;
  }
  const mimeType = pickVideoMimeType();
  if (!mimeType) {
    chrome.runtime.sendMessage({ action: "VideoUnavailable", reason: "unsupported" });
    return;
  }
  try {
    videoTrack.contentHint = 'detail';
  } catch (error) {
    // not settable in this browser
  }
  videoStopReason = null;
  videoRecorder = new MediaRecorder(new MediaStream([videoTrack]), { mimeType, ...VIDEO_RECORDER_OPTIONS });
  let chain = Promise.resolve();
  videoRecorder.ondataavailable = (event) => {
    if (!event.data || !event.data.size) return;
    chain = chain.then(async () => {
      const data = await bytesToBase64DataUrl(await event.data.arrayBuffer(), "video/mp4");
      chrome.runtime.sendMessage({ action: "VideoChunk", data });
    });
  };
  videoRecorder.onstop = () => {
    chain = chain.then(() => chrome.runtime.sendMessage({ action: "VideoStopped", reason: videoStopReason || "stopped" }));
    videoRecorder = null;
  };
  videoRecorder.start(VIDEO_TIMESLICE_MS);
  chrome.runtime.sendMessage({ action: "VideoStarted", mimeType });
};

const stopVideo = (reason) => {
  if (!videoRecorder) return;
  if (videoStopReason === null) videoStopReason = reason;
  if (videoRecorder.state !== 'inactive') videoRecorder.stop();
};

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
    stopVideo("stopped");
    videoTrack = null;

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

const startStreaming = async (withVideo = false) => {
  try {
    audioContext = new window.AudioContext({
      sampleRate: 8000
    });
    videoTrack = null;
    /* Get display media works */
    displayStream = await navigator.mediaDevices.getDisplayMedia({
      preferCurrentTab: true,
      // Recorded as video only with the panel's switch on: 5 frames a second,
      // the long edge at most 1920, and no switching to another tab part-way
      // through, which would record something other than the meeting.
      video: withVideo ? VIDEO_CONSTRAINTS : true,
      ...(withVideo ? { surfaceSwitching: "exclude" } : {}),
      audio: {
        noiseSuppression: true,
        autoGainControl: true,
        echoCancellation: true,
      }
    });

    if (withVideo) {
      const [track] = displayStream.getVideoTracks();
      const surface = track && track.getSettings ? track.getSettings().displaySurface : undefined;
      if (track && surface === "browser") {
        videoTrack = track;
        track.addEventListener("ended", () => stopVideo("ended"));
      } else {
        // A window or the whole screen: the audio goes on, with no video.
        // The panel is told when it asks for the video (StartVideo).
        console.log("LMA: the shared surface is not the meeting tab", surface || "unknown");
      }
    }

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