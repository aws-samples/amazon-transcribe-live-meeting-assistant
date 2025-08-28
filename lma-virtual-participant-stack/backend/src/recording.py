#
# 
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
# SPDX-License-Identifier: MIT-0
# 
#
import os
import re
import boto3
import details
import wave
from urllib.parse import urljoin, quote


REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


def convert_to_wav(in_filename, tmp_wav_filename):
    wf = wave.open(tmp_wav_filename, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)  # 2 bytes per sample (16-bit audio)
    wf.setframerate(16000)
    with open(in_filename, 'rb') as input_file:
        audio_data = input_file.read()
        wf.writeframes(audio_data)
    wf.close()
    print(f"Wavefile saved to {tmp_wav_filename}")


def upload_file_to_s3(local_file_path, bucket_name, s3_file_path):
    s3_client = boto3.client('s3')

    print(
        f"Starting upload of {local_file_path} to s3://{bucket_name}/{s3_file_path}")
    s3_client.upload_file(local_file_path, bucket_name, s3_file_path)
    print(f"File uploaded successfully to s3://{bucket_name}/{s3_file_path}")


def generate_recording_url(s3_wav_path):
    base_url = f'https://{details.recordings_bucket_name}.s3.{REGION}.amazonaws.com/'
    encoded_path = quote(s3_wav_path)
    recording_url = urljoin(base_url, encoded_path)
    return recording_url


def delete_file(filename):
    try:
        os.remove(filename)
        print(f"File {filename} has been successfully deleted.")
    except Exception as e:
        print(f"An error occurred while trying to delete {filename}: {str(e)}")


def posixify_filename(filename: str) -> str:
    # Replace all invalid characters with underscores
    regex = r'[^a-zA-Z0-9_.]'
    posix_filename = re.sub(regex, '_', filename)
    # Remove leading and trailing underscores
    posix_filename = re.sub(r'^_+', '', posix_filename)
    posix_filename = re.sub(r'_+$', '', posix_filename)
    return posix_filename


def upload_recording_to_S3():
    convert_to_wav(details.tmp_recording_filename, details.tmp_wav_filename)
    WAV_FILE_NAME = posixify_filename(
        f"{details.meeting_name_with_timestamp}.wav")
    s3_wav_path = f"{details.recording_file_prefix}{WAV_FILE_NAME}"
    upload_file_to_s3(details.tmp_wav_filename,
                      details.recordings_bucket_name, s3_wav_path)
    delete_file(details.tmp_recording_filename)
    delete_file(details.tmp_wav_filename)
    url = generate_recording_url(s3_wav_path)
    return url
