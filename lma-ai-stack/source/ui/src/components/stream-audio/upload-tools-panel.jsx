/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Upload Audio</h2>;
const content = (
  <>
    <p>
      Upload a pre-recorded audio or video file and LMA will transcribe it with Amazon Transcribe, generate a meeting
      summary with Amazon Bedrock, and make the meeting searchable alongside your live-streamed meetings.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Supports common audio and video formats (wav, mp3, mp4, m4a, webm, flac, ogg, amr)</li>
      <li>Direct browser-to-S3 upload — file never transits a Lambda or API Gateway</li>
      <li>Optional speaker diarization (2&ndash;30 speakers)</li>
      <li>Transcribe &amp; summary pipeline is the same one used by live meetings</li>
      <li>Meeting appears in the Meetings List with a full detail page once transcription completes</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/stream-audio/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Stream &amp; Upload Audio Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/transcription-and-translation/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Transcription &amp; Translation
        </a>
      </li>
    </ul>
  </>
);

const UploadToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default UploadToolsPanel;
