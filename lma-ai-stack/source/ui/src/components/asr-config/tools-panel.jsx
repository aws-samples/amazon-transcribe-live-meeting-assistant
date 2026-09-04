/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Badge, HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = (
  <h2>
    ASR Configuration <Badge color="severity-medium">Experimental</Badge>
  </h2>
);
const content = (
  <>
    <p>
      Which engine transcribes streaming meetings (Stream Audio, the Chrome extension, the Desktop Capture apps) and
      which transcribes Virtual Participants. Both settings apply to the next meeting that starts.
    </p>
    <h3>Amazon Transcribe</h3>
    <p>The default and the recommended engine for production: redaction, custom vocabulary, 30+ languages.</p>
    <h3>On-demand ASR &amp; diarization</h3>
    <p>
      Transcribes and identifies speakers in one pass on a MicroVM launched per meeting, so several people sharing one
      microphone come apart reliably. English only; none of the Amazon Transcribe features above. A meeting whose
      MicroVM cannot start falls back to Amazon Transcribe.
    </p>
    <h3>Virtual Participant voice separation</h3>
    <p>
      Speakers are named from the meeting roster. Turn this on only when one attendee carries several people; the engine
      then labels their voices <i>Name (spk_0)</i>, <i>Name (spk_1)</i>.
    </p>
    <h3>Notes</h3>
    <ul>
      <li>
        The speaker similarity threshold and minimum utterance length are measured for the model bundle and baked into
        the ASR image, so there is nothing to tune here.
      </li>
      <li>Speaker labels are per meeting and per audio channel, not identities.</li>
      <li>
        The Desktop Capture apps can still force an engine per run with <code>--asr-engine</code>.
      </li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/microvm-asr/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> On-demand ASR &amp; Speaker Diarization
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
