# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The Call fields a meeting write selects, so its onUpdateCall message is whole.

AppSync publishes to onUpdateCall the fields the mutation selected, and an open
meeting page merges each message into the meeting it shows
(use-calls-graphql-api.js mergeCallUpdate), so a write that leaves a field out
would blank it there. Every Call mutation this server makes therefore selects
what the meeting page's subscription selects: the selection of
ui/src/graphql/queries/onUpdateCall.js, which
lma-ai-stack/test/test_mcp_tool_schemas.py holds this copy to.

A document names the selection as ``CALL_FIELDS_PLACEHOLDER`` and is completed
with ``with_call_fields``, so that it stays a plain string the AppSync contract
test (test_appsync_contract.py) can read and validate against the schema.
"""

SUBSCRIBED_CALL_FIELDS = """
  PK
  SK
  CallId
  AgentId
  CallSummaryText
  CallSummaryState
  SpeakerIdentities
  Agenda
  AgendaSource
  AgendaUpdatedBy
  AgendaUpdatedAt
  Labels {
    Key
    Value
  }
  Audience
  MeetingMetadataUpdatedAt
  MeetingContext
  MeetingContextUpdatedBy
  MeetingContextUpdatedAt
  Checklist {
    ItemId
    Text
    Kind
    Priority
    Source
    Status
    StatusSource
    Evidence {
      SegmentId
      StartTime
      Quote
    }
    Note
    UpdatedBy
    UpdatedAt
  }
  ChecklistUpdatedAt
  Status
  UpdatedAt
  CreatedAt
  CustomerPhoneNumber
  SystemPhoneNumber
  RecordingUrl
  VideoRecordingUrl
  RecordingUrls
  RecordingUrlSessions
  VideoRecordingUrls
  VideoRecordingUrlSessions
  SessionNumber
  SessionOffsetSeconds
  SessionStartedNumber
  Sessions {
    SessionNumber
    OffsetSeconds
    StartedAt
  }
  Owner
  SharedWith
  TotalConversationDurationMillis
  DetectedLanguages
  TranscriptionPause
  Sentiment {
    OverallSentiment {
      REMOTE
      LOCAL
      AGENT
      CALLER
    }
    SentimentByPeriod {
      QUARTER {
        REMOTE {
          BeginOffsetMillis
          EndOffsetMillis
          Score
        }
        LOCAL {
          BeginOffsetMillis
          EndOffsetMillis
          Score
        }
        AGENT {
          BeginOffsetMillis
          EndOffsetMillis
          Score
        }
        CALLER {
          BeginOffsetMillis
          EndOffsetMillis
          Score
        }
      }
    }
  }

"""

CALL_FIELDS_PLACEHOLDER = "__SUBSCRIBED_CALL_FIELDS__"


def with_call_fields(document: str) -> str:
    """The GraphQL document with the subscribed Call fields in place of the placeholder."""
    return document.replace(CALL_FIELDS_PLACEHOLDER, SUBSCRIBED_CALL_FIELDS)
