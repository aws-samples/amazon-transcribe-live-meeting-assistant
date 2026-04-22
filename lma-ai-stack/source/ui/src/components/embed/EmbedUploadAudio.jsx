/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedUploadAudio — Embeddable upload-only audio page (no navigation chrome).
 * Renders the shared <StreamAudio /> component locked into upload mode.
 * Live streaming is handled separately by EmbedStreamAudio because it needs
 * special autoStart/postMessage handling.
 */
import React, { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';

import StreamAudio from '../stream-audio/StreamAudio';

const EmbedUploadAudio = ({ sendToParent }) => {
  const sentLoadedRef = useRef(false);

  useEffect(() => {
    if (!sentLoadedRef.current && sendToParent) {
      sentLoadedRef.current = true;
      sendToParent({ type: 'LMA_EMBED_LOADED', component: 'upload-audio' });
    }
  }, [sendToParent]);

  return (
    <div className="embed-upload-audio" style={{ padding: '1rem' }}>
      <StreamAudio mode="upload" />
    </div>
  );
};

EmbedUploadAudio.propTypes = {
  sendToParent: PropTypes.func,
};

EmbedUploadAudio.defaultProps = {
  sendToParent: null,
};

export default EmbedUploadAudio;
