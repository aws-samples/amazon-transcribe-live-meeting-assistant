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
      <b>This engine is experimental and not production ready.</b> Its transcript quality is below Amazon
      Transcribe&apos;s and defaults may change between releases. Amazon Transcribe remains the recommended engine for
      production meetings.
    </p>
    <p>
      Two switches decide where the on-demand ASR &amp; speaker diarization engine is used. Both are read at the start
      of each meeting, so a change needs no redeploy.
    </p>
    <h3>Default engine</h3>
    <p>
      Off, every meeting uses Amazon Transcribe. On, streaming meetings and Virtual Participants use the on-demand
      engine unless a Stream Audio meeting picks an engine itself. A MicroVM that cannot start falls back to Amazon
      Transcribe automatically.
    </p>
    <h3>Virtual Participant voice separation</h3>
    <p>
      A Virtual Participant names speakers from the meeting roster. Turn this on only when one attendee carries several
      people — a conference room, or a shared screen playing a recording — and the engine will label the voices behind
      that name as <i>Name (spk_0)</i>, <i>Name (spk_1)</i>.
    </p>
    <h3>Why nothing else is here</h3>
    <ul>
      <li>
        The similarity threshold and minimum utterance length were measured for the deployed bundle&apos;s speaker model
        and are baked into the ASR image. A guessed or borrowed number fragments one person into several or merges
        several into one, which is why they are not offered as settings.
      </li>
      <li>Each audio channel is diarized independently, so a voice on the mic is never a tab speaker.</li>
      <li>Speaker labels are per meeting, not identities, and are least accurate in the first minute.</li>
      <li>
        A meeting on this engine does not use Amazon Transcribe, so redaction, custom vocabulary, custom language models
        and language identification do not apply to it.
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
