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
      Two switches decide where the on-demand ASR &amp; speaker diarization engine is used. Both apply to the next
      meeting that starts.
    </p>
    <h3>Default engine</h3>
    <p>
      Off: every meeting uses Amazon Transcribe. On: streaming meetings and Virtual Participants use the on-demand
      engine unless a Stream Audio meeting picks one itself.
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
      <li>Meetings on this engine do not use Amazon Transcribe features such as redaction or custom vocabulary.</li>
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
