# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import os
import asyncio
import details
import kds
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent
import sounddevice as sd
from botocore.exceptions import BotoCoreError, ClientError

# globals
current_speaker = "none"


class MyEventHandler(TranscriptResultStreamHandler):
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        for result in transcript_event.transcript.results:
            kds.send_add_transcript_segment(current_speaker, result)


async def write_audio(transcribe_stream, recording_stream):
    loop = asyncio.get_event_loop()
    input_queue = asyncio.Queue()

    def callback(indata, frame_count, time_info, status):
        loop.call_soon_threadsafe(
            input_queue.put_nowait, (bytes(indata), status))

    with sd.RawInputStream(
        channels=1,
        samplerate=16000,
        callback=callback,
        blocksize=3200,  # roughly 100ms of audio data
        dtype='int16'
    ):
        while details.start:
            indata, status = await input_queue.get()
            await transcribe_stream.input_stream.send_audio_event(audio_chunk=indata)
            recording_stream.write(indata)
        await transcribe_stream.input_stream.end_stream()
        recording_stream.close()


async def transcribe():
    print("Transcribe starting")
    
    # Update VP status to ACTIVE when transcription begins
    vp_id = os.environ.get('VIRTUAL_PARTICIPANT_ID')
    if vp_id:
        try:
            from status_manager import VirtualParticipantStatusManager
            status_manager = VirtualParticipantStatusManager(vp_id)
            status_manager.set_active()
            print(f"VP {vp_id} status: ACTIVE (transcription started)")
        except Exception as e:
            print(f"Failed to update VP status to ACTIVE: {e}")
    
    kds.send_start_meeting()

    if details.transcribe_language_code in ["identify-language", "identify-multiple-languages"]:
        print("WARNING: Language identification option has been selected, but is not supported in Virtual Participant")
        language_code = details.transcribe_preferred_language if details.transcribe_preferred_language != "None" else "en-US"
    else:
        language_code = details.transcribe_language_code

    print(f"Using Transcribe language code: {language_code}")

    max_retries = 5
    retry_delay = 5  # seconds
    session_id = None
    transcribe_client = TranscribeStreamingClient(region="us-east-1")

    for attempt in range(max_retries):
        try:
            if session_id is None:
                # Initial attempt without session_id
                transcribe_stream = await transcribe_client.start_stream_transcription(
                    language_code=language_code,
                    media_sample_rate_hz=16000,
                    media_encoding="pcm"
                )
                session_id = transcribe_stream.response.session_id
                print(f"Started new transcription session with ID: {session_id}")
            else:
                # Retry attempt with existing session_id
                transcribe_stream = await transcribe_client.start_stream_transcription(
                    language_code=language_code,
                    media_sample_rate_hz=16000,
                    media_encoding="pcm",
                    session_id=session_id
                )
                print(f"Resumed transcription session with ID: {session_id}")

            recording_stream = open(details.tmp_recording_filename, "wb")
            await asyncio.gather(
                write_audio(transcribe_stream, recording_stream),
                MyEventHandler(transcribe_stream.output_stream).handle_events()
            )
            print("Transcribe completed successfully")
            break
        except (BotoCoreError, ClientError) as e:
            print(f"Transcribe error (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
            else:
                print("Max retries reached. Transcribe stopped.")
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            print("Transcribe stopped due to unexpected error.")
            break

    print("Transcribe function completed")


async def speaker_change(speaker):
    global current_speaker
    current_speaker = speaker
    print(f"Speaker: {current_speaker}")
