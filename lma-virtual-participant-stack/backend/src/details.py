
import os
import datetime

meeting_platform = os.environ['MEETING_PLATFORM']
meeting_id = os.environ['MEETING_ID']
meeting_password = os.environ['MEETING_PASSWORD']
meeting_name = os.environ['MEETING_NAME']
meeting_name_with_timestamp = meeting_name + '-' + \
    datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S.%f')[:-3]

email_sender = os.getenv('EMAIL_SENDER', '')
email_receiver = os.getenv('EMAIL_RECEIVER', '')

lma_user = os.getenv('LMA_USER', 'unknown user')

transcribe_language_code = os.getenv('TRANSCRIBE_LANGUAGE_CODE', 'en-US')
transcribe_preferred_language = os.getenv(
    'TRANSCRIBE_PREFERRED_LANGUAGE', 'en-US')

intro_message = os.environ['INTRO_MESSAGE'].replace('{LMA_USER}', lma_user)
start_recording_message = os.environ['START_RECORDING_MESSAGE'].replace(
    '{LMA_USER}', lma_user)
stop_recording_message = os.environ['STOP_RECORDING_MESSAGE'].replace(
    '{LMA_USER}', lma_user)
exit_message = os.environ['EXIT_MESSAGE'].replace('{LMA_USER}', lma_user)
lma_identity = os.environ['LMA_IDENTITY'].replace('{LMA_USER}', lma_user)

should_record_call = os.getenv('SHOULD_RECORD_CALL', 'true') == 'true'
recordings_bucket_name = os.getenv('RECORDINGS_BUCKET_NAME')
recording_file_prefix = os.getenv('RECORDING_FILE_PREFIX', 'lma-audio-wav/')
if should_record_call:
    tmp_recording_filename = "/tmp/chunk_recording.bin"
    tmp_wav_filename = "/tmp/chunk_recording.wav"
else:
    tmp_recording_filename = "/dev/null"

waiting_timeout = 300000  # 5 minutes
meeting_timeout = 21600000  # 6 hours

start = True

start_command = "START"
pause_command = "PAUSE"
end_command = "END"

intro_messages = [
    intro_message
]
start_messages = [
    start_recording_message
]
pause_messages = [
    stop_recording_message
]
exit_messages = [
    exit_message
]

messages = []
attachments = {}
captions = []
speakers = []
