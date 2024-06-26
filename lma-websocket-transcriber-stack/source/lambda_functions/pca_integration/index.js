/*
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
 */
/* eslint-disable no-console */
/* eslint-disable no-await-in-loop */
/* eslint-disable no-restricted-syntax */
/* eslint-disable no-param-reassign */

// Add a '/' to S3 or HTML paths if needed
const formatPath = function(path) {
  let pathOut = path;
  if (path.length > 0 && path.charAt(path.length - 1) != "/") {
    pathOut += "/";
  }
  return pathOut;
};

const { DynamoDBClient, GetItemCommand } = require('@aws-sdk/client-dynamodb');
const { S3Client, CopyObjectCommand } = require('@aws-sdk/client-s3');
const { KinesisClient, PutRecordCommand } = require('@aws-sdk/client-kinesis');
const REGION = process.env.REGION || 'us-east-1';
const s3Client = new S3Client({ region: REGION });
const dynamoClient = new DynamoDBClient({ region: REGION });
const kinesisClient = new KinesisClient({ region: REGION });

const KINESIS_STREAM_NAME = process.env.KINESIS_STREAM_NAME || '';
const TRANSCRIBER_CALL_EVENT_TABLE_NAME = process.env.TRANSCRIBER_CALL_EVENT_TABLE_NAME || '';
const LCA_BUCKET_NAME = process.env.LCA_BUCKET_NAME || '';
const CALL_ANALYTICS_FILE_PREFIX = formatPath(process.env.CALL_ANALYTICS_FILE_PREFIX || 'lca-call-analytics/');
const PCA_S3_BUCKET_NAME = process.env.PCA_S3_BUCKET_NAME || '';
const PCA_TRANSCRIPTS_PREFIX = formatPath(process.env.PCA_TRANSCRIPTS_PREFIX || '');
const PCA_AUDIO_PLAYBACK_FILE_PREFIX = formatPath(process.env.PCA_AUDIO_PLAYBACK_FILE_PREFIX || '');
const PCA_WEB_APP_URL = formatPath(process.env.PCA_WEB_APP_URL || '');
const PCA_WEB_APP_CALL_PATH_PREFIX = formatPath(process.env.PCA_WEB_APP_CALL_PATH_PREFIX || '');
const IS_CONTENT_REDACTION_ENABLED = (process.env.IS_CONTENT_REDACTION_ENABLED || 'true') === 'true';


function mkTcaFilename(sessionData) {
  let f = `TCA_GUID_${sessionData.callId}`;
  f = `${f}_CUST_${sessionData.fromNumber}`;
  if (sessionData.agentId) {
    f = `${f}_AGENT_${sessionData.agentId}`;
  }
  const date = sessionData.callStreamingStartTime.replace(/:/g,"-");
  f = `${f}_${date}`;
  return f;
}

// TODO - Refactor to use new TCA Post Call event - now includes Transcript file and Media File Uris.
function getAnalyticsOutputUri(sessionId, suffix) {
  const analyticsfolder = (IS_CONTENT_REDACTION_ENABLED) ? "redacted-analytics/" : "analytics/";
  const uri = `/${LCA_BUCKET_NAME}/${CALL_ANALYTICS_FILE_PREFIX}${analyticsfolder}${sessionId}${suffix}`;
  return uri;
}

const getSessionDataFromDdb = async function getSessionDataFromDdb(dynamoClient, sessionId) {
  // Set the parameters
  const params = {
    Key: {
      PK: {
        S: `sd#${sessionId}`,
      },
      SK: {
        S: 'TRANSCRIBE SESSION',
      },
    },
    TableName: TRANSCRIBER_CALL_EVENT_TABLE_NAME,
  };
  console.log('GetItem params: ', JSON.stringify(params));
  const command = new GetItemCommand(params);
  let sessionData;
  try {
    const data = await dynamoClient.send(command);
    console.log('GetItem result: ', JSON.stringify(data));
    sessionData = JSON.parse(data.Item.SessionData.S);
  } catch (error) {
    console.log('Error retrieving sessionData - Possibly invalid sessionId?: ', error);
  }
  return sessionData;
};

const writePcaUrlToKds = async function writePcaUrlToKds(kinesisClient, sessionData) {
  const filename = mkTcaFilename(sessionData);
  console.log('Writing TCA URL To KDS');
  const now = new Date().toISOString();
  const eventType = 'ADD_PCA_URL';
  const pcaUrl = `${PCA_WEB_APP_URL}${PCA_WEB_APP_CALL_PATH_PREFIX}${filename}.json`;
  const putObj = {
    CallId: sessionData.callId,
    PcaUrl: pcaUrl,
    EventType: eventType.toString(),
    CreatedAt: now,
  };
  const putParams = {
    StreamName: KINESIS_STREAM_NAME,
    PartitionKey: sessionData.callId,
    Data: Buffer.from(JSON.stringify(putObj)),
  };
  const putCmd = new PutRecordCommand(putParams);
  console.log('Sending ADD_PCA_URL event on KDS: ', JSON.stringify(putObj));
  try {
    await kinesisClient.send(putCmd);
  } catch (error) {
    console.error('Error writing ADD_PCA_URL event', error);
  }
}

const copyAudioRecordingToPca = async function copyAudioRecordingToPca(s3Client, sessionData) {
  const copyParms = {
    Bucket: PCA_S3_BUCKET_NAME,
    CopySource: encodeURI(getAnalyticsOutputUri(sessionData.sessionId, '.wav')),
    Key: PCA_AUDIO_PLAYBACK_FILE_PREFIX + mkTcaFilename(sessionData) + ".wav",
  }
  console.log("Copying post call analytics Recording to PCA: ", copyParms);
  try {
    data = await s3Client.send(new CopyObjectCommand(copyParms));
    console.log("Done copying recording.");
  } catch (err) {
    console.error('S3 copy error: ', JSON.stringify(err));
  }
}

const copyPostCallAnalyticsToPca = async function copyPostCallAnalyticsToPca(s3Client, sessionData) {
  const filename = mkTcaFilename(sessionData);
  const copyParms = {
    Bucket: PCA_S3_BUCKET_NAME,
    CopySource: encodeURI(getAnalyticsOutputUri(sessionData.sessionId, '.json')),
    Key: PCA_TRANSCRIPTS_PREFIX + filename + ".json",
  }
  console.log("Copying post call analytics Transcript to PCA: ", copyParms);
  try {
    data = await s3Client.send(new CopyObjectCommand(copyParms));
    console.log("Done copying transcript.");
  } catch (err) {
    console.error('S3 copy error: ', JSON.stringify(err));
  }
}

const handler = async function handler(event, context) {
  console.log("Event: ", JSON.stringify(event));
  const sessionId = event.detail.StreamingSessionId;
  let job_completed = true;
  if (event.detail.PostCallStatus != "COMPLETED" && event.detail.PostStreamStatus != "COMPLETED") {
    console.log("ERROR Job failed - Failure reason:", event.detail.FailureReason);
    job_completed = false;
  }
  const sessionData = await getSessionDataFromDdb(dynamoClient, sessionId);
  if (!sessionData) {
    console.log("ERROR: Can't continue - no sessionData found.");
  }
  if (!job_completed) {
    console.log("ERROR: Can't continue - transcribe post call job failed.");
  }
  if (sessionData && job_completed) {
    await copyAudioRecordingToPca(s3Client, sessionData);
    await copyPostCallAnalyticsToPca(s3Client, sessionData);
    await writePcaUrlToKds(kinesisClient, sessionData);
  }
  return;
};

exports.handler = handler;