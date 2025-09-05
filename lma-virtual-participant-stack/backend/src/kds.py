# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import os
import boto3
import json
import datetime
import details
import bisect

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
KINESIS_STREAM_NAME = os.getenv("KINESIS_STREAM_NAME")
CALLID = details.meeting_name_with_timestamp
USER_NAME = details.lma_identity
USER_ACCESS_TOKEN = os.getenv("USER_ACCESS_TOKEN")
USER_ID_TOKEN = os.getenv("USER_ID_TOKEN")
USER_REFRESH_TOKEN = os.getenv("USER_REFRESH_TOKEN")

KINESIS = boto3.client('kinesis', region_name=REGION)


def get_aws_date_now():
    now = datetime.datetime.now()
    aws_datetime = now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return aws_datetime


# globals for item parsing
current_speaker_name = None
speakers = []
starttimes = []


def add_item_to_segment(item, segments):
    global speakers
    segment_index = bisect.bisect_left(starttimes, item.start_time) - 1
    if segment_index < 0:
        segment_index = 0
    segment_id = f'{speakers[segment_index]}-{starttimes[segment_index]}'
    if not segment_id in segments:
        segments[segment_id] = {
            'SegmentId': segment_id,
            'Speaker': speakers[segment_index],
            'StartTime': starttimes[segment_index],
            'EndTime': item.end_time,
            'Transcript': ''
        }
    elif item.item_type == 'pronunciation':
        # add a space between words
        segments[segment_id]['Transcript'] += " "
    segments[segment_id]['EndTime'] = item.end_time
    segments[segment_id]['Transcript'] += item.content
    return segments


def process_transcription_results(speaker_name, result):
    global current_speaker_name, speakers, starttimes
    segments = {}
    if current_speaker_name != speaker_name:
        # start time of new speaker is the start time of the last item in the results
        current_speaker_name = speaker_name
        speakers.append(speaker_name)
        last_item = result.alternatives[0].items[-1]
        starttimes.append(last_item.start_time)
    alternative = result.alternatives[0]
    for item in alternative.items:
        segments = add_item_to_segment(item, segments)
        if os.getenv('DEBUG'):
            print(
                f"DEBUG: Item {item.start_time, item.end_time, item.content}")
            print(f"DEBUG: Speakers {speakers}")
            print(f"DEBUG: Starttimes {starttimes}")
            print(f"DEBUG: Segments {segments}")
    # if it's a non partial result, then re-initialize globals
    if not result.is_partial:
        print("INFO: Non partial result - Resetting speaker and start times")
        current_speaker_name = None
        speakers = []
        starttimes = []
    return segments


def send_add_transcript_segment(speaker_name, result):
    print("Process speaker changes to identify segments within result")
    segments = process_transcription_results(speaker_name, result)
    for segment in segments.values():
        if os.getenv('DEBUG'):
            print(
                f"Sending ADD_TRANSCRIPT_SEGMENT event to Kinesis. Segment: {segment}")
        try:
            add_transcript_segment = {
                'EventType': 'ADD_TRANSCRIPT_SEGMENT',
                'CallId': CALLID,
                'Channel': 'CALLER',
                'SegmentId': segment['SegmentId'],
                'StartTime': segment['StartTime'] if segment['StartTime'] is not None else 0,
                'EndTime': segment['EndTime'] if segment['EndTime'] is not None else 0,
                'Transcript': segment['Transcript'],
                'IsPartial': result.is_partial,
                'CreatedAt': get_aws_date_now(),
                'UpdatedAt': get_aws_date_now(),
                'Sentiment': None,
                'TranscriptEvent': None,
                'UtteranceEvent': None,
                'Speaker': segment['Speaker'],
                'AccessToken': USER_ACCESS_TOKEN,
                'IdToken': USER_ID_TOKEN,
                'RefreshToken': USER_REFRESH_TOKEN,
            }
            # Write the messages to the Kinesis Data Stream
            response = KINESIS.put_record(
                StreamName=KINESIS_STREAM_NAME,
                PartitionKey=CALLID,
                Data=json.dumps(add_transcript_segment).encode('utf-8')
            )
            print(
                f"Sent ADD_TRANSCRIPT_SEGMENT event to Kinesis.")
        except Exception as e:
            print(
                f"Error sending ADD_TRANSCRIPT_SEGMENT event to Kinesis: {e}")


def send_start_meeting():
    try:
        start_call_event = {
            'EventType': 'START',
            'CallId': CALLID,
            'CustomerPhoneNumber': 'Customer Phone',
            'SystemPhoneNumber': 'System Phone',
            'AgentId': USER_NAME,
            'CreatedAt': get_aws_date_now(),
            'AccessToken': USER_ACCESS_TOKEN,
            'IdToken': USER_ID_TOKEN,
            'RefreshToken': USER_REFRESH_TOKEN,
        }
        print(
            f"Sending start meeting event to Kinesis. Event: {start_call_event}")

        # Write the messages to the Kinesis Data Stream
        response = KINESIS.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=CALLID,
            Data=json.dumps(start_call_event).encode('utf-8')
        )
        print(
            f"Sent start meeting event to Kinesis.")
    except Exception as e:
        print(f"Error sending start meeting event to Kinesis: {e}")


def send_end_meeting():
    try:
        start_call_event = {
            'EventType': 'END',
            'CallId': CALLID,
            'CustomerPhoneNumber': 'Customer Phone',
            'SystemPhoneNumber': 'System Phone',
            'CreatedAt': get_aws_date_now(),
            'AccessToken': USER_ACCESS_TOKEN,
            'IdToken': USER_ID_TOKEN,
            'RefreshToken': USER_REFRESH_TOKEN,
        }
        print(
            f"Sending end meeting event to Kinesis. Event: {start_call_event}")

        # Write the messages to the Kinesis Data Stream
        response = KINESIS.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=CALLID,
            Data=json.dumps(start_call_event).encode('utf-8')
        )
        print(
            f"Sent end meeting event to Kinesis.")
    except Exception as e:
        print(f"Error sending start meeting event to Kinesis: {e}")


def send_call_recording(url):
    try:
        call_recording_event = {
            'EventType': 'ADD_S3_RECORDING_URL',
            'CallId': CALLID,
            'RecordingUrl': url,
            'AccessToken': USER_ACCESS_TOKEN,
            'IdToken': USER_ID_TOKEN,
            'RefreshToken': USER_REFRESH_TOKEN,
        }
        print(
            f"Sending call recording event to Kinesis. Event: {call_recording_event}")

        # Write the messages to the Kinesis Data Stream
        response = KINESIS.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=CALLID,
            Data=json.dumps(call_recording_event).encode('utf-8')
        )
        print(
            f"Sent call recording event to Kinesis.")
    except Exception as e:
        print(f"Error sending call recording event to Kinesis: {e}")
