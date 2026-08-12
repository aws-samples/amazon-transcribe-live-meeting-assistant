/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>ASR Configuration</h2>;
const content = (
  <>
    <p>
      Tune the on-demand ASR &amp; speaker diarization engine without redeploying. Every field is an optional override
      on the CloudFormation parameters, read at the start of each meeting.
    </p>
    <h3>Tuning order when one person appears as several speakers</h3>
    <ol>
      <li>
        <b>Speaker similarity threshold</b> — the usual cause. It is specific to the speaker model: measured at 0.2 for
        the default TitaNet embedder, where different speakers scored at most 0.107 and the same speaker 0.25–0.5.
      </li>
      <li>
        <b>Minimum utterance for speaker ID</b> — raise it. Embeddings from one- or two-word utterances are unreliable
        and are where phantom speakers come from.
      </li>
      <li>
        <b>Maximum speakers per channel</b> — a hard cap. It bounds the symptom rather than fixing the operating point,
        so reach for it last, and only when the meeting size is known.
      </li>
    </ol>
    <h3>Notes</h3>
    <ul>
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
