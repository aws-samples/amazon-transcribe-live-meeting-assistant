import os
import boto3
import json
import datetime
import details

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
KINESIS_STREAM_NAME = os.getenv("KINESIS_STREAM_NAME")
MEETING_NAME = details.meeting_name
LMA_MEETING_NAME = MEETING_NAME + '-' + \
    datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S.%f')[:-3]

KINESIS = boto3.client('kinesis', region_name=REGION)


def get_aws_date_now():
    now = datetime.datetime.now()
    aws_datetime = now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return aws_datetime


def send_add_transcript_segment(current_speaker, result):
    print("Sending add transcript segment event to Kinesis")
    try:
        transcript = result.alternatives[0].transcript
        add_transcript_segment = {
            'EventType': 'ADD_TRANSCRIPT_SEGMENT',
            'CallId': LMA_MEETING_NAME,
            'Channel': 'CALLER',
            'SegmentId': f'CALLER-${result.start_time}',
            'StartTime': result.start_time if result.start_time is not None else 0,
            'EndTime': result.end_time if result.end_time is not None else 0,
            'Transcript': transcript,
            'IsPartial': result.is_partial,
            'CreatedAt': get_aws_date_now(),
            'UpdatedAt': get_aws_date_now(),
            'Sentiment': None,
            'TranscriptEvent': None,
            'UtteranceEvent': None,
            'Speaker': current_speaker
        }
        # Write the messages to the Kinesis Data Stream
        response = KINESIS.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=LMA_MEETING_NAME,
            Data=json.dumps(add_transcript_segment).encode('utf-8')
        )
        print(
            f"Sent add transcript segment event to Kinesis.")
    except Exception as e:
        print(
            f"Error sending add transcript segment event to Kinesis: {e}")


def send_start_meeting():
    try:
        start_call_event = {
            'EventType': 'START',
            'CallId': LMA_MEETING_NAME,
            'CustomerPhoneNumber': 'Customer Phone',
            'SystemPhoneNumber': 'System Phone',
            'AgentId': 'test-agent',
            'CreatedAt': get_aws_date_now()
        }
        print(
            f"Sending start meeting event to Kinesis. Event: {start_call_event}")

        # Write the messages to the Kinesis Data Stream
        response = KINESIS.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=LMA_MEETING_NAME,
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
            'CallId': LMA_MEETING_NAME,
            'CustomerPhoneNumber': 'Customer Phone',
            'SystemPhoneNumber': 'System Phone',
            'CreatedAt': get_aws_date_now()
        }
        print(
            f"Sending end meeting event to Kinesis. Event: {start_call_event}")

        # Write the messages to the Kinesis Data Stream
        response = KINESIS.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=LMA_MEETING_NAME,
            Data=json.dumps(start_call_event).encode('utf-8')
        )
        print(
            f"Sent end meeting event to Kinesis.")
    except Exception as e:
        print(f"Error sending start meeting event to Kinesis: {e}")
